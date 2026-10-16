from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from companion.background_agent.settings import BackgroundAgentSettings
from companion.core.dev_ports import load_dev_ports
from companion.core.runtime_paths import (
    BACKEND_ROOT,
    CONSOLE_ROOT,
    MODEL_CATALOG_PATH,
    SEED_CHARACTER_CARDS_ROOT,
    SHARED_ROOT,
    USER_DATA_ROOT,
)
from companion.llm.config import LlmSettings
from companion.observability.prompt_audit import PromptAuditSettings


ENV_YAML_PATH = Path(os.environ.get("NEARMATE_ENV_PATH", str(BACKEND_ROOT / "config" / "env.yaml"))).expanduser().resolve()
DEV_PORTS = load_dev_ports()


class Settings(BaseSettings):
    """控制层配置。

    默认值先从 shared/default-settings.json 读取，
    再允许 backend/config/env.yaml 覆盖非密钥运行配置，保证用户密钥只来自控制台服务密钥流程。
    当前同时共享最近一次成功选择的人物模型目录名，供桌面端后续恢复上次角色使用。
    """

    model_config = SettingsConfigDict(extra="ignore")

    control_host: str = DEV_PORTS["backend"].host
    control_port: int = DEV_PORTS["backend"].port

    followup_timeout_s: float = 300.0  # 追问等待超时秒数，用于限制连续追问场景的等待窗口。
    show_speech_bubbles: bool = True  # 是否展示气泡文案，用于控制桌面端对话气泡显示开关。
    followup_toggle_shortcut_enabled: bool = True  # 是否启用桌面全局追问热区切换快捷键。
    followup_toggle_shortcut: str = "CommandOrControl+Alt+Space"  # 桌面端用于切换追问热区的 Electron accelerator。
    avatar_visibility_shortcut_enabled: bool = True  # 是否启用桌宠召回/收起全局快捷键。
    avatar_visibility_shortcut: str = "CommandOrControl+Q"  # 桌面端用于召回或收起桌宠的 Electron accelerator。
    selection_capture_shortcut_enabled: bool = True  # 是否启用捕获当前选中内容为材料的全局快捷键。
    selection_capture_shortcut: str = "CommandOrControl+Alt+C"  # 桌面端用于捕获选中文本或图片的 Electron accelerator。

    selected_live2d_model_id: str = "Haru"  # 默认选中的 Live2D 模型标识，供桌面端恢复上次角色外观。
    user_call_name: str = "用户"  # 旧版用户设置兜底字段；新版对用户的称呼由角色卡 meta 管理。
    user_self_intro: str = ""  # 用户在控制台主动填写的自我简介，只作为显式配置进入提示词，不由聊天自动生成。
    user_extra_notes: str = ""  # 用户在控制台主动填写的自由补充，用于补充少量稳定背景信息。
    bootstrap_db_path: str = str(USER_DATA_ROOT / "companion.sqlite3")  # 启动凭据数据库路径沿用原 SQLite 文件，升级时保留已有密钥。
    user_settings_path: str = str(USER_DATA_ROOT / "user-settings.json")  # 用户最后一次选择的本地设置文件路径，和数据库同属本机运行数据。
    background_agent: BackgroundAgentSettings = Field(default_factory=BackgroundAgentSettings)  # 后台智能体非密钥设置；密钥继续保存在 bootstrap SQLite。
    background_agent_runtime_dir: str = str(USER_DATA_ROOT / "agent-runtime")  # Claude Code 私有配置和会话目录，不能由 Console 覆盖。
    background_agent_usage_db_path: str = str(USER_DATA_ROOT / "background-agent-usage.sqlite3")  # 去内容化的后台工作用量独立长期保存，不与凭据或会话数据混用。
    background_agent_workspace_root: str = ""  # 留空时由后台智能体工作区模块解析系统“文档”目录。
    background_agent_asset_root: str = str(BACKEND_ROOT / "assets" / "agent-workspace")  # 随产品分发的 CLAUDE.md 与 Skills 资产根目录。
    tool_waiting_policies_path: str = str(USER_DATA_ROOT / "tool-waiting-policies.json")  # 工具等待反馈策略独立配置文件，避免污染扁平用户设置。
    character_cards_dir: str = str(USER_DATA_ROOT / "character-cards")  # 角色卡资产目录，保存可编辑人格卡与当前激活卡状态。
    character_card_seed_dir: str = str(SEED_CHARACTER_CARDS_ROOT)  # 安装包内置的只读角色卡素材，只在首次启动复制到用户目录。
    console_dist_dir: str = str(CONSOLE_ROOT)  # 控制台前端构建产物目录，供后端静态资源服务直接挂载。
    backend_ckpt_dir: str = str(BACKEND_ROOT / "ckpt")  # 后端模型资产根目录固定指向发行清单声明的 Backend 根目录。
    streamvox_model_dir: str = str(USER_DATA_ROOT / "streamvox" / "ckpts")  # 与 StreamVox_HOME/ckpts 对齐，TTS 权重只能写入用户数据目录。
    voice_setup_path: str = str(USER_DATA_ROOT / "voice-setup.json")  # 受管语音配置任务和已确认素材的持久状态。
    execution_config_path: str = str(USER_DATA_ROOT / "execution-config.json")  # 语音配置服务保存实际执行后端和模型目录。
    model_catalog_path: str = str(MODEL_CATALOG_PATH)  # 语音配置服务读取的固定模型文件清单。
    wakeword_model_dir: str = str(BACKEND_ROOT / "ckpt" / "wakeword")  # 唤醒词模型目录由实时语音链路加载，默认随清单声明的 Backend 根目录分发。
    smart_turn_model_path: str = str(BACKEND_ROOT / "ckpt" / "smart-turn-v3.2-gpu.onnx")  # 智能打断模型文件使用发行清单声明的 Backend 根目录。
    streamvox_reference_audio_path: str = str(BACKEND_ROOT / "ckpt" / "streamvox" / "Condition3.wav")  # StreamVox 参考音频默认放在发行清单声明的 Backend 资产目录内。
    llm: LlmSettings = Field(default_factory=LlmSettings)  # 已验证 LLM 接入配置；每项职责只允许绑定显式兼容矩阵中的模型规格。
    prompt_audit: PromptAuditSettings = Field(default_factory=PromptAuditSettings)  # 完整 Prompt 审查默认关闭，只由本地 env.yaml 显式开启。
    background_agent_terminal_diagnostics_enabled: bool = False  # 开发期 Background SDK 消息终端输出；只允许由本地 env.yaml 显式开启。
    qweather_api_host: str = ""  # 和风天气 API Host 来自和风控制台；用户通过 Console 服务密钥面板配置。
    qweather_api_key: str = ""  # 和风天气 API KEY 默认留空，避免把外部服务凭据写入仓库。
    qweather_timeout_s: float = 5.0  # 和风天气请求超时秒数，避免前台工具长时间阻塞对话链路。
    streamvox_device: str = "auto"  # StreamVox 默认设备交给 SDK 自动选择，控制台只暴露 auto、cpu、gpu，不处理多卡编号。
    streamvox_llama_backend: str | None = None  # StreamVox GGUF 模型默认不显式指定 llama.cpp 后端，控制台可显式切到 cuda 或 vulkan。
    # 说话人白名单确认由 console 显式开启；没有用户录音时始终保持关闭。
    speaker_verification_enabled: bool = False
    # cam++ 说话人嵌入模型，已随仓库 ckpt 分发。
    speaker_verification_model_path: str = str(
        BACKEND_ROOT / "ckpt" / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
    )
    # 白名单音频路径列表唯一来自 console 录音，不再内置开发者样本。
    speaker_whitelist_audio_paths: list[str] = Field(default_factory=list)
    audio_input_device_id: str | None = None  # 用户在 Console 手动选择的麦克风设备标识；None 表示不固定设备。
    audio_input_device_name: str | None = None  # 用户在 Console 手动选择的麦克风名称，用作 id 不稳定时的诊断和兜底匹配。
    audio_output_reference_device_id: str | None = None  # 用户在 Console 手动选择的扬声器参考 loopback 设备标识。
    audio_output_reference_device_name: str | None = None  # 用户在 Console 手动选择的扬声器参考 loopback 名称。
    audio_device_mode: str = "system_default"  # 音频设备选择模式：system_default 使用系统默认，manual 使用 Console 保存的设备。

    livekit_url: str = f"ws://{DEV_PORTS['livekit'].host}:{DEV_PORTS['livekit'].port}"  # 本地 LiveKit Server 地址，Desktop 与 Backend 媒体会话通过它进入同一房间。
    livekit_api_key: str = ""  # LiveKit 凭据必须由开发配置或外部运行环境注入，发行包不携带共享密钥。
    livekit_api_secret: str = ""  # 空凭据表示音频服务尚未配置，禁止用开发密钥伪装成可用状态。
    livekit_room_prefix: str = "companion-v1"  # V1 本地实时媒体房间名前缀，用于把产品房间与其它本地 LiveKit 测试隔离。
    livekit_media_session_identity: str = "companion-backend-agent"  # Backend 媒体会话的固定外部身份，保留历史值以维持 LiveKit 连接行为。
    livekit_desktop_identity_prefix: str = "desktop"  # desktop participant 身份前缀，每次签发 session 时追加唯一后缀避免身份冲突。
    livekit_aec_identity_prefix: str = "desktop-aec"  # AEC sidecar participant 身份前缀，只允许 sidecar 发布 clean mic 音轨，避免 renderer 原始麦克风进入实时识别链路。
    assistant_request_handoff_timeout_s: float = Field(default=1.0, gt=0.0)  # 媒体会话交接时等待旧世代请求双终态的确认窗口秒数。

    @classmethod
    def load_shared_defaults(cls) -> dict:
        """从 shared 目录读取默认设置。"""

        return json.loads((SHARED_ROOT / "default-settings.json").read_text(encoding="utf-8"))

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """定制配置来源优先级。

        pydantic-settings 默认让初始化参数优先级最高；进程环境变量是安装启动器注入外部凭据的边界，必须排在 env.yaml 前面。
        env.yaml 只覆盖非敏感发行默认值，shared JSON 仍然是最低优先级的产品默认值。
        返回的顺序从高到低生效，最后的类字段默认值作为兜底；如果 shared JSON 缺字段，BaseSettings 会继续使用字段声明中的默认值。
        """

        return (
            init_settings,
            env_settings,
            _EnvYamlSource(settings_cls),
            _SharedDefaultsSource(settings_cls),
            file_secret_settings,
        )


class _EnvYamlSource(PydanticBaseSettingsSource):
    """backend/config/env.yaml 配置来源。

    该来源只承载非用户密钥类运行配置；用户可填写的外部服务密钥必须走控制台服务密钥仓储。
    """

    def get_field_value(self, field, field_name: str) -> tuple[Any, str, bool]:
        """按字段读取 env.yaml 配置。

        pydantic-settings 抽象类要求实现该方法；本来源一次性返回完整字典，因此单字段读取永远表示未命中。
        """

        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """读取并返回 env.yaml 中的配置字典。

        Returns:
            env.yaml 不存在时返回空字典；存在时必须是 YAML 对象。
        """

        if not ENV_YAML_PATH.exists():
            return {}

        raw_payload = yaml.safe_load(ENV_YAML_PATH.read_text(encoding="utf-8"))
        if raw_payload is None:
            return {}
        if not isinstance(raw_payload, dict):
            raise ValueError(f"env.yaml 根节点必须是对象：{ENV_YAML_PATH}")
        return raw_payload


class _SharedDefaultsSource(PydanticBaseSettingsSource):
    """shared/default-settings.json 配置来源。

    settings_cls 由 pydantic-settings 传入，用于绑定当前 Settings 类型；调用时返回共享默认值字典。
    该来源放在 env.yaml 后面，因此只为缺失字段提供低优先级默认值，不会覆盖用户显式配置。
    """

    def get_field_value(self, field, field_name: str) -> tuple[Any, str, bool]:
        """按字段读取共享默认值。

        pydantic-settings 抽象类要求实现该方法；本来源一次性返回完整字典，因此单字段读取永远表示未命中。
        """

        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """返回共享默认值字典。

        输出直接来自 Settings.load_shared_defaults；JSON 解析或文件读取异常会向外抛出，避免静默使用错误配置启动。
        """

        return Settings.load_shared_defaults()
