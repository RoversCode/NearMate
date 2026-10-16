from __future__ import annotations

from pathlib import Path
import os
from threading import RLock

from companion.character_cards.store import CharacterCardStoreError
from companion.core.assistant_process_supervisor import AssistantProcessSupervisor
from companion.core.broadcast import BroadcastHub
from companion.core.streamvox_runtime_config import (
    development_voice_fingerprint,
    read_prepared_character_voice,
)
from companion.llm.client_factory import LlmClientFactory
from companion.llm.request_intent import LlmInvocationIntent
from companion.observability.prompt_audit import PromptAuditRecorder
from companion.wake_response.service import WakeResponseConfig, WakeResponseService


class WakeResponseConsoleService:
    """管理角色卡唤醒回复的控制台读写、生成和合成。

    Args:
        hub: 控制面广播和角色卡资产中心。
        assistant_process_supervisor: 当前助手子进程所有者。
        prompt_audit_recorder: 当前 Backend 主进程的 Prompt 审查记录器。
    """

    def __init__(
        self,
        *,
        hub: BroadcastHub,
        assistant_process_supervisor: AssistantProcessSupervisor,
        prompt_audit_recorder: PromptAuditRecorder,
    ) -> None:
        self.hub = hub
        self.assistant_process_supervisor = assistant_process_supervisor
        self.prompt_audit_recorder = prompt_audit_recorder
        # 长合成在工作线程执行；写事务串行，避免另一线程从旧配置保存并覆盖刚提交的条目。
        self._mutation_lock = RLock()

    def synthesize_wav(self, card_id: str, text: str) -> bytes:
        """用当前 StreamVox 音色合成唤醒回复 wav。"""

        card = self.hub.character_cards.load_card(card_id)
        reference_audio = self.hub.character_card_assets.resolve_voice_reference_audio(card)
        prepared = None
        try:
            prepared = read_prepared_character_voice(
                card=card,
                reference_audio=reference_audio,
                execution_config_path=Path(self.hub.settings.execution_config_path),
                voice_setup_path=Path(self.hub.settings.voice_setup_path),
            )
        except (RuntimeError, OSError):
            if (
                os.environ.get("NEARMATE_PACKAGED") == "1"
                or Path(self.hub.settings.execution_config_path).is_file()
                and Path(self.hub.settings.voice_setup_path).is_file()
            ):
                raise
            # 源码开发态允许直接使用 StreamVox 的模型名/角色缓存；正式安装包必须
            # 经过语音配置校验，避免把开发回退误带入发行环境。
            prepared = None
        runtime = self.assistant_process_supervisor.require_for_streamvox_management()
        state = runtime.get_streamvox_runtime_state(timeout=5.0)
        if state.get("model") != card.meta.voice_binding.streamvox_model or state.get("role_name") != card.meta.voice_binding.role_name:
            raise RuntimeError("运行语音与当前角色已确认配置不一致，请先应用角色语音")
        if prepared is not None and (
            state.get("voice_directory") != str(prepared.voice_directory)
            or state.get("onnx_provider") != prepared.onnx_provider
            or state.get("llama_backend") != prepared.llama_backend
        ):
            raise RuntimeError("运行语音与当前角色已确认配置不一致，请先应用角色语音")
        preview = runtime.preview_streamvox_role(
            role_name=card.meta.voice_binding.role_name,
            text=text,
            timeout=30.0,
        )
        wav_bytes = preview.get("wav_bytes", b"")
        if not isinstance(wav_bytes, bytes):
            raise RuntimeError("StreamVox preview response missing wav bytes")
        return wav_bytes

    def service_for_card(self, card_id: str) -> WakeResponseService:
        """创建绑定到指定角色卡目录的唤醒回复服务。

        Args:
            card_id: 角色卡编号。

        Returns:
            只读写该角色卡唤醒回复音频目录的服务实例。
        """

        bundled_library = getattr(self.hub.character_card_assets, "bundled_wake_responses", None)
        return WakeResponseService(
            asset_dir=self.hub.character_card_assets.wake_response_audio_dir(card_id),
            synthesize_wav=lambda text: self.synthesize_wav(card_id, text),
            voice_fingerprint_provider=lambda: self.voice_fingerprint_for_card(card_id),
            bundled_audio_validator=(
                lambda item: bundled_library.is_audio_current(card_id, item)
            ) if bundled_library is not None else None,
        )

    def voice_fingerprint_for_card(self, card_id: str) -> str | None:
        """读取仍与指定角色素材相符的已确认音色指纹。

        Args:
            card_id: 要读取语音缓存资格的角色卡编号。
        Returns:
            可用于缓存校验的指纹；未就绪或素材已改变时返回空值。
        """

        card = None
        reference_audio = None
        try:
            card = self.hub.character_cards.load_card(card_id)
            reference_audio = self.hub.character_card_assets.resolve_voice_reference_audio(card)
            prepared = read_prepared_character_voice(
                card=card,
                reference_audio=reference_audio,
                execution_config_path=Path(self.hub.settings.execution_config_path),
                voice_setup_path=Path(self.hub.settings.voice_setup_path),
            )
        except (CharacterCardStoreError, RuntimeError, OSError):
            if (
                os.environ.get("NEARMATE_PACKAGED") == "1"
                or Path(self.hub.settings.execution_config_path).is_file()
                and Path(self.hub.settings.voice_setup_path).is_file()
            ):
                return None
            if card is None:
                return None
            return development_voice_fingerprint(card=card, reference_audio=reference_audio)
        return prepared.context["fingerprint"]

    def audio_path_for_card(self, card_id: str, file_name: str) -> Path:
        """只允许试听配置中登记且属于当前音色的唤醒回复文件。

        Args:
            card_id: 音频所属的角色卡。
            file_name: 控制台请求的音频文件名。
        Returns:
            已核验的音频路径；旧音频或未登记文件抛出 KeyError。
        """

        self.hub.character_cards.load_card(card_id)
        service = self.service_for_card(card_id)
        config = service.normalize_config(self.hub.character_card_assets.load_wake_response(card_id))
        for items in config.buckets.values():
            for item in items:
                if item.audio_path and Path(item.audio_path).name == file_name and service.is_audio_current(item):
                    return service.resolve_audio_path(item.audio_path)
        raise KeyError("wake response audio not found or no longer current")

    def audio_url_base(self, card_id: str) -> str:
        """返回指定角色卡唤醒回复音频 URL 前缀。"""

        return f"/api/character-cards/{card_id}/wake-response/audio"

    def active_config(self) -> WakeResponseConfig:
        """读取当前激活角色卡的唤醒回复配置。"""

        active_card_id = self.hub.character_cards.load_active_card_id()
        service = self.service_for_card(active_card_id)
        return service.normalize_config(self.hub.character_card_assets.load_wake_response(active_card_id))

    def config_payload(self, card_id: str) -> dict[str, object]:
        """读取指定角色卡唤醒回复配置并转换为控制台载荷。"""

        self.hub.character_cards.load_card(card_id)
        service = self.service_for_card(card_id)
        config = service.normalize_config(self.hub.character_card_assets.load_wake_response(card_id))
        return service.to_payload(
            config,
            include_audio_url=True,
            audio_url_base=self.audio_url_base(card_id),
        )

    def save_config_for_card(self, card_id: str, saved_config: WakeResponseConfig) -> None:
        """保存指定角色卡的唤醒回复配置。"""

        service = self.service_for_card(card_id)
        self.hub.character_card_assets.save_wake_response(
            card_id,
            service.to_payload(saved_config),
        )

    def generate_texts(self, card_id: str) -> dict[str, object]:
        """调用 LLM 生成四时段唤醒回复短句。"""

        character_card = self.hub.character_cards.load_card(card_id)

        # 组装角色示例对白作为语气参照——这是最直接的"TA 怎么说话"的样板
        dialogue_snippets: list[str] = []
        for dialogue in character_card.voiceprint.example_dialogues.items[:4]:
            dialogue_snippets.append(
                f"用户：{dialogue.user}\n{character_card.meta.name}：{dialogue.char}"
            )
        example_dialogues_block = "\n\n".join(dialogue_snippets) if dialogue_snippets else "（无示例）"

        # 组装情绪分级词表，让模型知道什么情绪用什么强度的词
        emotion_grading_lines: list[str] = []
        for level, expression in character_card.voiceprint.emotion_grading.items():
            emotion_grading_lines.append(f"  {level} → {expression}")
        emotion_grading_block = "\n".join(emotion_grading_lines) if emotion_grading_lines else "（无）"

        # 组装句式习惯，指导短句的断句、标点、语气词密度
        sentence_habits_block = (
            "\n".join(f"  - {habit}" for habit in character_card.voiceprint.sentence_habits)
            if character_card.voiceprint.sentence_habits
            else "（无）"
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是为虚拟桌面伴侣写台词的文案。你的任务是根据角色人设，为\"用户唤醒伴侣\"这个时刻，"
                    "写出自然、有辨识度的一句语音短句。\n\n"
                    "## 场景\n"
                    "用户在桌面点击唤醒虚拟伴侣。伴侣根据当前时段说一句短语音——"
                    "这句话要让用户觉得\"是 TA 本人在跟我打招呼\"，不是系统提示音、不是客服问候。\n\n"
                    "## 输出格式\n"
                    "严格输出 json 对象，不要 Markdown、不要解释文字。\n"
                    "四个时段（morning / afternoon / evening / night）各恰好 5 条中文短句。\n"
                    "每条 ≤10 个汉字，口语化、适合 TTS 朗读，像脱口而出而不是写出来的。\n\n"
                    "## 各时段语气方向\n"
                    "- morning（清晨 5:00–11:00）：刚醒不久，带点慵懒或元气，短短几个字把人拉进新一天\n"
                    "- afternoon（中午 11:00–17:00）：日常随性，\"在呢，说呗\"的陪伴感，不隆重\n"
                    "- evening（傍晚 17:00–22:00）：松弛、卸下一天的劲儿，像回家打个招呼\n"
                    "- night（深夜 22:00–5:00）：压低声量、温柔收敛，不是亢奋是\"我还在，慢慢说\"\n\n"
                    "## 核心原则\n"
                    "- 每条短句必须是\"这个人\"会说的话，不是通用模板填名字\n"
                    "- 活用口癖和语气词，但不要每条都塞满——自然分布\n"
                    "- 不同时段的 5 条之间要有变化（措辞、情绪、亲密度），不能是同一句话的 5 个变体\n"
                    "- 唤醒是\"轻轻地出现\"，不是隆重登场；克制、不演\n\n"
                    "## EXAMPLE JSON OUTPUT\n"
                    "{\n"
                    '  "buckets": {\n'
                    '    "morning": ["早呀，醒啦", "嗯…早", "今天起挺早嘛", "早，我在呢", "新一天啦"],\n'
                    '    "afternoon": ["嗯？说呗", "在呢", "听着呢", "下午好呀", "怎么啦"],\n'
                    '    "evening": ["回来啦", "晚上好呀", "今天怎样", "终于晚上了", "嗯，在呢"],\n'
                    '    "night": ["还没睡？", "嗯…在", "别太晚哦", "我在，慢慢说", "该睡啦"]\n'
                    "  }\n"
                    "}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"请根据以下角色设定，为 TA 写四个时段的唤醒短句。\n\n"
                    f"【角色名】{character_card.meta.name}\n"
                    f"【一句话人设】{character_card.meta.title}\n\n"
                    f"【说话风格】\n{character_card.voiceprint.speech_style}\n\n"
                    f"【常用口癖】\n{'、'.join(character_card.voiceprint.catchphrases) if character_card.voiceprint.catchphrases else '（无）'}\n\n"
                    f"【情绪分级词】（什么情绪 → 用什么强度的词）\n{emotion_grading_block}\n\n"
                    f"【句式习惯】\n{sentence_habits_block}\n\n"
                    f"【语气参照对白】下面是 TA 和别人对话的真实片段，请严格模仿其中的语调、节奏、用词习惯：\n{example_dialogues_block}\n\n"
                    f"【生成红线】\n{character_card.model_constraints.persona_forbidden_rules}\n\n"
                    f"【用户称呼】{character_card.meta.user_call_name or '（未设置）'}\n"
                    f"【用户简介】{self.hub.settings.user_self_intro or '（未设置）'}\n"
                    f"【用户补充】{self.hub.settings.user_extra_notes or '（未设置）'}"
                ),
            },
        ]
        client = LlmClientFactory(
            self.hub.settings.llm,
            prompt_audit_recorder=self.prompt_audit_recorder,
        ).client("character_authoring")
        try:
            raw_text = client.complete_text(
                messages=messages,
                intent=LlmInvocationIntent.json_object(
                    thinking_level="off",
                    max_output_tokens=1600,
                ),
                audit_source="console",
                audit_operation="wake_response_generation",
            ).strip()
        finally:
            # 角色创作不是常驻运行时，每次请求创建的连接池必须在本次调用结束时回收。
            client.close()
        try:
            payload = __import__("json").loads(raw_text)
        except ValueError as exception:
            raise RuntimeError("LLM wake response generation returned invalid JSON") from exception
        normalized_config = self.hub.wake_response_service.normalize_config(payload)
        return {
            "buckets": {
                bucket: [item.text for item in items[:5]]
                for bucket, items in normalized_config.buckets.items()
            }
        }

    def save_payload(self, card_id: str, payload_buckets: dict[str, list[dict[str, object]]]) -> dict[str, object]:
        """串行提交整卡草稿，只为变更或失效的短句合成音频。

        Args:
            card_id: 要保存的角色卡编号。
            payload_buckets: 用户提交的四时段完整草稿。
        Returns:
            本次提交后的控制台配置快照。
        """
        with self._mutation_lock:
            self.hub.character_cards.load_card(card_id)
            service = self.service_for_card(card_id)
            current_config = service.normalize_config(self.hub.character_card_assets.load_wake_response(card_id))
            next_config = service.normalize_config({"buckets": payload_buckets})
            saved_config = service.save_config(current_config=current_config, next_config=next_config)
            self.save_config_for_card(card_id, saved_config)
            return self.config_payload(card_id)

    def synthesize_item(self, card_id: str, item_id: str, *, bucket: str, text: str) -> dict[str, object]:
        """串行合成并保存一条短句，避免并发写入覆盖其他条目。

        Args:
            card_id: 音频所属角色卡编号。
            item_id: 目标短句编号。
            bucket: 目标时段。
            text: 当前草稿文字。
        Returns:
            该条短句提交后的控制台快照。
        """
        with self._mutation_lock:
            self.hub.character_cards.load_card(card_id)
            service = self.service_for_card(card_id)
            current_config = service.normalize_config(self.hub.character_card_assets.load_wake_response(card_id))
            saved_config = service.synthesize_item(
                current_config=current_config, bucket=bucket, item_id=item_id, text=text
            )
            self.save_config_for_card(card_id, saved_config)
            payload = self.config_payload(card_id)
            for item in payload["buckets"][bucket]:
                if item["id"] == item_id:
                    return item
            raise KeyError("wake response item not found")

    def delete_item(self, card_id: str, item_id: str) -> None:
        """在同一写事务内删除短句和受管音频，不与合成任务交错。

        Args:
            card_id: 目标角色卡编号。
            item_id: 要删除的短句编号。
        Returns:
            无；目标不存在时抛出 KeyError。
        """
        with self._mutation_lock:
            self.hub.character_cards.load_card(card_id)
            service = self.service_for_card(card_id)
            current_config = service.normalize_config(self.hub.character_card_assets.load_wake_response(card_id))
            saved_config = service.remove_item(current_config, item_id)
            self.save_config_for_card(card_id, saved_config)
