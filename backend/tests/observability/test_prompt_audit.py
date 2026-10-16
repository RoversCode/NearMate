from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from companion.core.settings import Settings
from companion.llm.chat_client import ChatCompletionsClient
from companion.llm.config import LlmRuntimeConfig
from companion.llm.model_registry import get_model_integration
from companion.llm.output_policies import NoOpOutputPolicy
from companion.llm.request_adapters import DeepSeekChatRequestAdapter
from companion.llm.request_intent import LlmInvocationIntent
from companion.llm.request_profiles import get_model_request_profile
from companion.observability import prompt_audit as prompt_audit_module
from companion.observability.prompt_audit import (
    PromptAuditRecorder,
    PromptAuditSettings,
)


def test_disabled_recorder_does_not_create_any_file(tmp_path: Path) -> None:
    """关闭审查时不得创建目录、更不用序列化完整 Prompt。"""

    output_dir = tmp_path / "prompt-audits"
    recorder = PromptAuditRecorder(
        enabled=False,
        output_dir=output_dir,
        max_runs=20,
        process_role="assistant-runtime",
    )

    result = recorder.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="initial",
        request={"messages": [{"role": "user", "content": "不要写入"}]},
    )

    assert result is None
    assert not output_dir.exists()


def test_settings_default_is_disabled_and_resolves_relative_output_dir() -> None:
    """代码默认关闭，但配置可以解析开发环境的相对输出目录。"""

    settings = PromptAuditSettings.model_validate(
        {
            "enabled": True,
            "output_dir": "backend/data/prompt-audits",
            "max_runs": 20,
        }
    )

    assert PromptAuditSettings().enabled is False
    assert settings.resolved_output_dir().as_posix().endswith(
        "/backend/data/prompt-audits"
    )


def test_application_settings_load_current_prompt_audit_switch() -> None:
    """开发 env.yaml 必须显式开启审查，并保留完整运行的默认数量。"""

    settings = Settings()

    assert settings.prompt_audit.enabled is True
    assert settings.prompt_audit.max_runs == 20
    assert settings.prompt_audit.resolved_output_dir().as_posix().endswith(
        "/backend/data/prompt-audits"
    )


def test_recorder_sanitizes_credentials_and_inline_images(tmp_path: Path) -> None:
    """审查文件保留请求结构，但绝不保留凭据或内联图片原文。"""

    recorder = _recorder(tmp_path, process_id=101)
    audit_path = recorder.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="initial",
        request={
            "api_key": "api-key-must-not-appear",
            "extra_body": {"authorization": "Bearer hidden-token"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看这张图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "inspect_image",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "api_key": {"type": "string"},
                            },
                        },
                    },
                }
            ],
        },
        trace_fields={"request_id": "request-a", "llm_call_id": "call-a"},
    )

    assert audit_path is not None
    markdown = audit_path.read_text(encoding="utf-8")
    assert "api-key-must-not-appear" not in markdown
    assert "hidden-token" not in markdown
    assert "data:image/png;base64,AAAA" not in markdown
    assert "[已屏蔽凭据]" in markdown
    assert "media_type=image/png" in markdown
    assert "bytes=3" in markdown
    assert '"api_key"' in markdown


def test_recorder_folds_remote_image_without_exposing_signed_url(
    tmp_path: Path,
) -> None:
    """远程图片只保留可关联摘要，签名 URL 不能进入审查文件。"""

    remote_url = (
        "https://cdn.example.test/private/photo.png"
        "?X-Amz-Credential=test-credential&X-Amz-Signature=secret-signature#preview"
    )
    recorder = _recorder(tmp_path, process_id=109)

    audit_path = recorder.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="initial",
        request={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": remote_url, "detail": "high"},
                        }
                    ],
                }
            ]
        },
        trace_fields={"llm_call_id": "call-remote-image"},
    )

    assert audit_path is not None
    markdown = audit_path.read_text(encoding="utf-8")
    assert remote_url not in markdown
    assert "test-credential" not in markdown
    assert "secret-signature" not in markdown
    assert "media_type=image/png" in markdown
    assert "bytes=unknown" in markdown
    assert (
        f"url_sha256={hashlib.sha256(remote_url.encode('utf-8')).hexdigest()}"
        in markdown
    )
    assert '"detail": "high"' in markdown


def test_recorder_sanitizes_tool_arguments_and_structured_results(
    tmp_path: Path,
) -> None:
    """工具参数和结构化结果中的凭据、图片引用都必须递归脱敏。"""

    argument_image_url = (
        "https://images.example.test/input.webp?token=argument-image-secret"
    )
    result_image_url = (
        "https://images.example.test/result.jpg?signature=result-image-secret"
    )
    recorder = _recorder(tmp_path, process_id=110)
    audit_path = recorder.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="tool_followup",
        request={
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-tool",
                            "type": "function",
                            "function": {
                                "name": "inspect_private_image",
                                "arguments": json.dumps(
                                    {
                                        "api_key": "argument-api-key",
                                        "nested": {"access_token": "argument-token"},
                                        "image_url": argument_image_url,
                                    }
                                ),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-tool",
                    "content": json.dumps(
                        {
                            "authorization": "Bearer result-token",
                            "image_urls": [result_image_url],
                        }
                    ),
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "inspect_private_image",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "api_key": {"type": "string"},
                                "image_url": {"type": "string"},
                            },
                        },
                    },
                }
            ],
        },
        trace_fields={"llm_call_id": "call-tool-sanitizing"},
    )

    assert audit_path is not None
    markdown = audit_path.read_text(encoding="utf-8")
    for secret in (
        "argument-api-key",
        "argument-token",
        "result-token",
        argument_image_url,
        result_image_url,
        "argument-image-secret",
        "result-image-secret",
    ):
        assert secret not in markdown
    assert markdown.count("[已屏蔽凭据]") >= 3
    assert markdown.count("url_sha256=") == 2
    # Schema 中的字段名是调试工具声明所必需的，不能被当成真实凭据值删除。
    assert '"api_key"' in markdown
    assert '"image_url"' in markdown


def test_recorder_replaces_malformed_tool_arguments_with_safe_summary(
    tmp_path: Path,
) -> None:
    """无法解析的工具参数不保留原文，只留下长度与稳定哈希。"""

    malformed_arguments = '{"api_key":"malformed-secret"'
    recorder = _recorder(tmp_path, process_id=111)
    audit_path = recorder.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="tool_followup",
        request={
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-malformed",
                            "type": "function",
                            "function": {
                                "name": "broken_tool",
                                "arguments": malformed_arguments,
                            },
                        }
                    ],
                }
            ]
        },
        trace_fields={"llm_call_id": "call-malformed-arguments"},
    )

    assert audit_path is not None
    markdown = audit_path.read_text(encoding="utf-8")
    assert malformed_arguments not in markdown
    assert "malformed-secret" not in markdown
    assert f"chars={len(malformed_arguments)}" in markdown
    assert (
        f"sha256={hashlib.sha256(malformed_arguments.encode('utf-8')).hexdigest()}"
        in markdown
    )


def test_recorder_assigns_unique_sequence_and_renders_chronological_navigation(
    tmp_path: Path,
) -> None:
    """并发记录时必须得到唯一序号，HTML 也按序号而不是线程完成顺序展示。"""

    recorder = _recorder(tmp_path, process_id=102)

    def record(sequence: int) -> Path | None:
        """提交一个拥有稳定调用编号的并发审查请求。"""

        return recorder.record_request(
            role="foreground",
            source="text_input",
            protocol="chat_completions",
            model="deepseek-flash",
            operation="initial",
            request={"messages": [{"role": "user", "content": str(sequence)}]},
            trace_fields={"llm_call_id": f"call-{sequence}"},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(record, range(16)))

    markdown_paths = sorted(path for path in paths if path is not None)
    assert [path.name[:6] for path in markdown_paths] == [
        f"{sequence:06d}" for sequence in range(1, 17)
    ]
    assert not list(markdown_paths[0].parent.glob(".*.tmp"))

    index = (markdown_paths[0].parent / "index.html").read_text(encoding="utf-8")
    positions = [index.index(f"#{sequence:06d}") for sequence in range(1, 17)]
    assert positions == sorted(positions)
    assert "用户文字 · Foreground 首轮" in index
    assert '<iframe id="detail" title="Prompt Markdown" sandbox></iframe>' in index


def test_recorder_keeps_only_latest_complete_runs(tmp_path: Path) -> None:
    """每个进程角色只保留最近指定数量的整次运行目录。"""

    for index in range(4):
        recorded_at = datetime(2026, 8, 17, 8, index, tzinfo=timezone.utc)
        recorder = PromptAuditRecorder(
            enabled=True,
            output_dir=tmp_path,
            max_runs=2,
            process_role="assistant-runtime",
            now=lambda recorded_at=recorded_at: recorded_at,
            id_factory=lambda index=index: f"run-{index}",
            process_id=200 + index,
            process_is_alive=lambda _process_id: False,
        )
        recorder.record_request(
            role="foreground",
            source="text_input",
            protocol="chat_completions",
            model="deepseek-flash",
            operation="initial",
            request={"messages": []},
            trace_fields={"llm_call_id": f"call-{index}"},
        )

    process_dir = tmp_path / "assistant-runtime"
    retained_runs = sorted((process_dir / "runs").iterdir())
    assert len(retained_runs) == 3
    assert all("pid-200" not in path.name for path in retained_runs)
    process_index = (process_dir / "index.html").read_text(encoding="utf-8")
    assert "pid-200" not in process_index
    assert "pid-201" in process_index
    assert "pid-202" in process_index
    assert "pid-203" in process_index


def test_retention_never_prunes_an_active_assistant_runtime(tmp_path: Path) -> None:
    """热替换短暂并存时，活动助手运行不能因保留数已满被删除。"""

    active_process_ids = {301}

    def is_process_alive(process_id: int) -> bool:
        """模拟多个仍在排空的助手子进程。"""

        return process_id in active_process_ids

    first = PromptAuditRecorder(
        enabled=True,
        output_dir=tmp_path,
        max_runs=1,
        process_role="assistant-runtime",
        now=lambda: datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc),
        id_factory=lambda: "run-first",
        process_id=301,
        process_is_alive=is_process_alive,
    )
    first.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="initial",
        request={"messages": []},
        trace_fields={"llm_call_id": "call-first"},
    )

    active_process_ids.add(302)
    second = PromptAuditRecorder(
        enabled=True,
        output_dir=tmp_path,
        max_runs=1,
        process_role="assistant-runtime",
        now=lambda: datetime(2026, 8, 17, 8, 1, tzinfo=timezone.utc),
        id_factory=lambda: "run-second",
        process_id=302,
        process_is_alive=is_process_alive,
    )
    second.record_request(
        role="foreground",
        source="text_input",
        protocol="chat_completions",
        model="deepseek-flash",
        operation="initial",
        request={"messages": []},
        trace_fields={"llm_call_id": "call-second"},
    )

    process_dir = tmp_path / "assistant-runtime"
    run_names = {path.name for path in (process_dir / "runs").iterdir()}
    assert len(run_names) == 2
    assert any("pid-301" in run_name for run_name in run_names)
    assert any("pid-302" in run_name for run_name in run_names)
    process_index = (process_dir / "index.html").read_text(encoding="utf-8")
    assert "pid-301" in process_index
    assert "pid-302" in process_index


def test_html_escapes_untrusted_metadata(tmp_path: Path) -> None:
    """运行索引必须把可能来自外部的模型和身份字段作为文本显示。"""

    recorder = _recorder(tmp_path, process_id=103)
    audit_path = recorder.record_request(
        role="<script>alert(1)</script>",
        source="<script>alert(2)</script>",
        protocol="chat_completions",
        model="<script>alert(3)</script>",
        operation="initial",
        request={"messages": []},
        trace_fields={"llm_call_id": "<script>alert(4)</script>"},
    )

    assert audit_path is not None
    index = (audit_path.parent / "index.html").read_text(encoding="utf-8")
    assert "<script>alert" not in index
    assert "&lt;script&gt;alert(3)&lt;/script&gt;" in index


def test_run_index_labels_every_audited_business_path(tmp_path: Path) -> None:
    """索引必须把所有业务调用类别翻译成可快速扫描的中文用途。"""

    recorder = _recorder(tmp_path, process_id=107)
    expected_labels = (
        ("foreground", "text_input", "initial", "用户文字 · Foreground 首轮"),
        (
            "foreground",
            "system_notice",
            "tool_followup",
            "System Notice · Foreground 工具后二轮",
        ),
        (
            "foreground",
            "timed_reminder_notice",
            "initial",
            "Reminder Notice · Foreground 首轮",
        ),
        ("summary", "runtime_memory", "compaction", "运行期记忆 · Summary 压缩"),
        ("cognitive", "activity", "execute", "Cognitive · 任务执行"),
        (
            "cognitive_result_repair",
            "activity",
            "repair",
            "Cognitive · Flash 结果修复",
        ),
        (
            "character_authoring",
            "backend_main",
            "wake_response_generation",
            "角色创作 · 唤醒回复",
        ),
    )

    paths = [
        recorder.record_request(
            role=role,
            source=source,
            protocol="chat_completions",
            model="model",
            operation=operation,
            request={"messages": []},
            trace_fields={"llm_call_id": f"call-{index}"},
        )
        for index, (role, source, operation, _label) in enumerate(
            expected_labels,
            start=1,
        )
    ]

    assert all(path is not None for path in paths)
    first_path = paths[0]
    assert first_path is not None
    index = (first_path.parent / "index.html").read_text(encoding="utf-8")
    for _role, _source, _operation, label in expected_labels:
        assert label in index


def test_precancelled_stream_creates_no_audit_file(tmp_path: Path) -> None:
    """预取消请求连 SDK 都不进入，也不得产生误导性的审查文件。"""

    sdk_calls = 0

    def create(**_kwargs: object) -> object:
        """记录异常的 SDK 调用。"""

        nonlocal sdk_calls
        sdk_calls += 1
        return iter(())

    cancellation_event = Event()
    cancellation_event.set()
    recorder = _recorder(tmp_path, process_id=104)

    assert list(
        _streaming_client(create, recorder).stream_message(
            messages=[],
            tools=[],
            intent=LlmInvocationIntent.text(),
            audit_source="text_input",
            audit_operation="initial",
            cancellation_event=cancellation_event,
        )
    ) == []
    assert sdk_calls == 0
    assert not list(tmp_path.iterdir())


def test_sdk_open_failure_is_audited_before_exception(tmp_path: Path) -> None:
    """SDK 打开失败也应留下最终请求，帮助定位失败前实际发送的 Prompt。"""

    def create(**_kwargs: object) -> object:
        """模拟 SDK 在网络打开阶段失败。"""

        raise RuntimeError("provider unavailable")

    recorder = _recorder(tmp_path, process_id=105)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        list(
            _streaming_client(create, recorder).stream_message(
                messages=[{"role": "user", "content": "请求正文"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "inspect_request",
                            "description": "检查最终审计请求。",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                intent=LlmInvocationIntent(tool_mode="auto"),
                audit_source="text_input",
                audit_operation="initial",
            )
        )

    markdown_paths = list(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    assert len(markdown_paths) == 1
    markdown = markdown_paths[0].read_text(encoding="utf-8")
    assert "请求正文" in markdown
    assert '"stream": true' in markdown
    assert "inspect_request" in markdown
    assert "## 模型结果" in markdown
    assert '"type": "RuntimeError"' in markdown
    assert "provider unavailable" in markdown


def test_repair_audit_preserves_complete_model_output(tmp_path: Path) -> None:
    """Flash 修复审查必须保留实际请求中的完整 Plus 原文。"""

    def create(**_kwargs: object) -> object:
        """返回一个正常结束的最小文本流。"""

        return iter([_chat_chunk(content="修复结果"), _chat_chunk(finish_reason="stop")])

    original_model_output = "完整结果\n<summary_tag>\n需要检查的原始摘要"
    repair_input = json.dumps(
        {
            "task_description": "核对当前事实",
            "model_output": original_model_output,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    recorder = _recorder(tmp_path, process_id=109)

    list(
        _streaming_client(create, recorder).stream_text(
            messages=[{"role": "user", "content": repair_input}],
            intent=LlmInvocationIntent.text(),
            audit_source="activity",
            audit_operation="repair",
        )
    )

    markdown_paths = list(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    assert len(markdown_paths) == 1
    markdown = markdown_paths[0].read_text(encoding="utf-8")
    assert repair_input in markdown
    assert "修复结果" in markdown
    assert "状态：`completed`" in markdown
    assert "Plus 原始正文未写入 Prompt Audit" not in markdown


def test_stream_message_audits_text_and_structured_tool_calls(tmp_path: Path) -> None:
    """Foreground 流应把正文和完整工具调用补进同一条审查记录。"""

    def create(**_kwargs: object) -> object:
        """返回同时包含正文和工具调用的完整流。"""

        return iter(
            [
                _chat_chunk(content="我来核对。"),
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content=None,
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call-a",
                                        function=SimpleNamespace(
                                            name="lookup_fact",
                                            arguments='{"query":"当前版本"}',
                                        ),
                                    )
                                ],
                            ),
                            finish_reason=None,
                        )
                    ]
                ),
                _chat_chunk(finish_reason="tool_calls"),
            ]
        )

    recorder = _recorder(tmp_path, process_id=113)
    list(
        _streaming_client(create, recorder).stream_message(
            messages=[{"role": "user", "content": "查一下当前版本"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "lookup_fact",
                        "description": "查询当前事实。",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ],
            intent=LlmInvocationIntent(tool_mode="auto"),
            audit_source="text_input",
            audit_operation="initial",
        )
    )

    markdown_paths = list(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    assert len(markdown_paths) == 1
    markdown = markdown_paths[0].read_text(encoding="utf-8")
    assert markdown.count("## 模型结果") == 1
    assert "我来核对。" in markdown
    assert "lookup_fact" in markdown
    assert "当前版本" in markdown
    assert "call-a" in markdown


def test_stream_consumption_error_audits_partial_text_before_reraising(
    tmp_path: Path,
) -> None:
    """厂商流中途失败时应记录已收到的正文和错误，再原样抛给业务层。"""

    def create(**_kwargs: object):  # type: ignore[no-untyped-def]
        """返回一段正文后模拟连接断开。"""

        yield _chat_chunk(content="已经收到的半段")
        raise ConnectionError("stream disconnected")

    recorder = _recorder(tmp_path, process_id=116)
    with pytest.raises(ConnectionError, match="stream disconnected"):
        list(
            _streaming_client(create, recorder).stream_text(
                messages=[{"role": "user", "content": "继续"}],
                intent=LlmInvocationIntent.text(),
                audit_source="activity",
                audit_operation="execute",
            )
        )

    markdown_paths = list(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    assert len(markdown_paths) == 1
    markdown = markdown_paths[0].read_text(encoding="utf-8")
    assert "已经收到的半段" in markdown
    assert '"type": "ConnectionError"' in markdown
    assert "stream disconnected" in markdown
    assert "状态：`error`" in markdown


def test_cancelled_stream_does_not_audit_partial_model_result(tmp_path: Path) -> None:
    """流式调用取消后只保留请求，不把已经到达的半截正文当成完整结果。"""

    cancellation_event = Event()

    def create(**_kwargs: object):  # type: ignore[no-untyped-def]
        """返回首段后触发取消的流。"""

        yield _chat_chunk(content="未完成正文")
        cancellation_event.set()
        yield _chat_chunk(finish_reason="stop")

    recorder = _recorder(tmp_path, process_id=114)
    list(
        _streaming_client(create, recorder).stream_text(
            messages=[{"role": "user", "content": "开始"}],
            intent=LlmInvocationIntent.text(),
            audit_source="activity",
            audit_operation="execute",
            cancellation_event=cancellation_event,
        )
    )

    markdown_paths = list(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    assert len(markdown_paths) == 1
    markdown = markdown_paths[0].read_text(encoding="utf-8")
    assert "## 模型结果" not in markdown
    assert "未完成正文" not in markdown


def test_complete_text_audits_response_before_return(tmp_path: Path) -> None:
    """非流式正文也应补写到原请求文件，不创建第二个调用序号。"""

    def create(**_kwargs: object) -> object:
        """返回一个正常的非流式响应。"""

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="压缩后的记忆",
                        tool_calls=None,
                    )
                )
            ]
        )

    recorder = _recorder(tmp_path, process_id=115)
    result = _streaming_client(create, recorder).complete_text(
        messages=[{"role": "user", "content": "请压缩"}],
        intent=LlmInvocationIntent.text(),
        audit_source="runtime_memory",
        audit_operation="compaction",
    )

    markdown_paths = list(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    assert result == "压缩后的记忆"
    assert len(markdown_paths) == 1
    markdown = markdown_paths[0].read_text(encoding="utf-8")
    assert "压缩后的记忆" in markdown
    assert "状态：`completed`" in markdown


def test_audit_write_failure_does_not_block_sdk_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查文件写盘失败必须 fail-open，真实模型调用仍照常执行。"""

    sdk_calls = 0

    def create(**_kwargs: object) -> object:
        """返回一个包含正常完成标记的最小流。"""

        nonlocal sdk_calls
        sdk_calls += 1
        return iter([_chat_chunk(content="正常回复"), _chat_chunk(finish_reason="stop")])

    def fail_write(_path: Path, _content: str) -> None:
        """模拟本地磁盘拒绝审查文件写入。"""

        raise OSError("disk unavailable")

    monkeypatch.setattr(prompt_audit_module, "_atomic_write_text", fail_write)
    recorder = _recorder(tmp_path, process_id=106)

    chunks = list(
        _streaming_client(create, recorder).stream_message(
            messages=[],
            tools=[],
            intent=LlmInvocationIntent.text(),
            audit_source="text_input",
            audit_operation="initial",
        )
    )

    assert sdk_calls == 1
    assert [chunk.text for chunk in chunks if chunk.kind == "text_delta"] == ["正常回复"]


def test_audit_lock_timeout_does_not_block_sdk_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """另一个进程占用审查目录时，到期必须跳过审计而不阻塞模型。"""

    sdk_calls = 0

    def create(**_kwargs: object) -> object:
        """返回一个包含正常完成标记的最小流。"""

        nonlocal sdk_calls
        sdk_calls += 1
        return iter([_chat_chunk(content="正常回复"), _chat_chunk(finish_reason="stop")])

    def fail_lock(_lock_file: object) -> None:
        """模拟跨进程锁在允许等待时间内始终不可用。"""

        raise TimeoutError("lock busy")

    monkeypatch.setattr(prompt_audit_module, "_lock_file", fail_lock)
    recorder = _recorder(tmp_path, process_id=108)

    chunks = list(
        _streaming_client(create, recorder).stream_message(
            messages=[],
            tools=[],
            intent=LlmInvocationIntent.text(),
            audit_source="text_input",
            audit_operation="initial",
        )
    )

    assert sdk_calls == 1
    assert [chunk.text for chunk in chunks if chunk.kind == "text_delta"] == ["正常回复"]
    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("index.html"))


def test_disabled_client_skips_lazy_audit_work_and_keeps_sdk_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭审查时不求值旁路工厂，也不接触渲染、锁或文件系统。"""

    sdk_calls = 0
    factory_calls = 0

    def create(**_kwargs: object) -> object:
        """返回一个正常结束的最小文本流。"""

        nonlocal sdk_calls
        sdk_calls += 1
        return iter([_chat_chunk(content="正常回复"), _chat_chunk(finish_reason="stop")])

    def audit_messages_factory() -> list[dict[str, Any]]:
        """一旦关闭态误求值就让测试明确失败。"""

        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("关闭态不应生成审查消息")

    def fail_render(**_kwargs: object) -> str:
        """一旦关闭态误进入脱敏渲染就让测试明确失败。"""

        raise AssertionError("关闭态不应渲染 Prompt")

    monkeypatch.setattr(
        prompt_audit_module,
        "render_prompt_audit_markdown",
        fail_render,
    )
    recorder = PromptAuditRecorder(
        enabled=False,
        output_dir=tmp_path / "prompt-audits",
        max_runs=20,
        process_role="assistant-runtime",
    )

    chunks = list(
        _streaming_client(create, recorder).stream_text(
            messages=[{"role": "user", "content": "真实请求"}],
            intent=LlmInvocationIntent.text(),
            audit_source="activity",
            audit_operation="repair",
            audit_messages_factory=audit_messages_factory,
        )
    )

    assert sdk_calls == 1
    assert factory_calls == 0
    assert [chunk.text for chunk in chunks if chunk.kind == "text_delta"] == [
        "正常回复"
    ]
    assert not (tmp_path / "prompt-audits").exists()


def test_disabled_client_preserves_sdk_open_exception(tmp_path: Path) -> None:
    """关闭审查后 SDK 打开异常必须原样传播，不能被旁路吞掉或重试。"""

    sdk_calls = 0

    def create(**_kwargs: object) -> object:
        """模拟 SDK 建流失败。"""

        nonlocal sdk_calls
        sdk_calls += 1
        raise RuntimeError("disabled provider failure")

    recorder = PromptAuditRecorder(
        enabled=False,
        output_dir=tmp_path / "prompt-audits",
        max_runs=20,
        process_role="assistant-runtime",
    )

    with pytest.raises(RuntimeError, match="disabled provider failure"):
        list(
            _streaming_client(create, recorder).stream_message(
                messages=[],
                tools=[],
                intent=LlmInvocationIntent.text(),
                audit_source="text_input",
                audit_operation="initial",
            )
        )

    assert sdk_calls == 1
    assert not (tmp_path / "prompt-audits").exists()


def test_record_and_call_sequence_matches_sdk_entry_order(tmp_path: Path) -> None:
    """启用审查时，Markdown 序号必须严格对应 SDK 回调进入顺序。"""

    recorder = _recorder(tmp_path, process_id=112)
    request_markers = [f"request-{index:02d}" for index in range(16)]
    sdk_entry_order: list[str] = []

    def record_and_call(marker: str) -> str:
        """记录一个请求，并在 SDK 回调开始时保存业务标记。"""

        def sdk_call() -> str:
            """模拟 SDK create 进入点。"""

            sdk_entry_order.append(marker)
            return marker

        result, _audit_path = recorder.record_request_and_call(
            role="foreground",
            source="text_input",
            protocol="chat_completions",
            model="deepseek-flash",
            operation="initial",
            request={"messages": [{"role": "user", "content": marker}]},
            trace_fields={"llm_call_id": f"call-{marker}"},
            sdk_call=sdk_call,
        )
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(record_and_call, request_markers))

    assert sorted(results) == sorted(request_markers)
    markdown_paths = sorted(tmp_path.glob("assistant-runtime/runs/*/*.md"))
    audited_order = []
    for markdown_path in markdown_paths:
        markdown = markdown_path.read_text(encoding="utf-8")
        audited_order.append(
            next(marker for marker in request_markers if marker in markdown)
        )
    assert audited_order == sdk_entry_order


def test_record_and_await_sequence_matches_async_sdk_entry_order(
    tmp_path: Path,
) -> None:
    """异步审计序号必须与 SDK transport 的真实写入顺序一致。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=113)
        request_markers = [f"async-request-{index:02d}" for index in range(12)]
        sdk_entry_order: list[str] = []

        async def record_and_await(marker: str) -> str:
            """并发提交一条审计和一条异步 SDK 请求。"""

            async def sdk_call() -> str:
                """记录 transport 真正开始写入的业务标记。"""

                sdk_entry_order.append(marker)
                await asyncio.sleep(0)
                return marker

            result, _audit_path = await recorder.record_request_and_await(
                role="background_agent",
                source="background_work",
                protocol="claude_agent_sdk",
                model="claude-sonnet-4-5",
                operation="execute",
                request={"messages": [{"role": "user", "content": marker}]},
                trace_fields={"llm_call_id": f"call-{marker}"},
                sdk_call=sdk_call,
            )
            return result

        results = await asyncio.gather(
            *(record_and_await(marker) for marker in request_markers)
        )

        assert sorted(results) == sorted(request_markers)
        markdown_paths = sorted(tmp_path.glob("assistant-runtime/runs/*/*.md"))
        audited_order = []
        for markdown_path in markdown_paths:
            markdown = markdown_path.read_text(encoding="utf-8")
            audited_order.append(
                next(marker for marker in request_markers if marker in markdown)
            )
        assert audited_order == sdk_entry_order

    asyncio.run(run())


def test_record_and_await_write_failure_calls_sdk_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步审计写盘失败必须 fail-open，且 SDK 只能调用一次。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=114)
        sdk_calls = 0
        first_path = recorder.record_request(
            role="foreground",
            source="text_input",
            protocol="chat_completions",
            model="deepseek-flash",
            operation="initial",
            request={"messages": [{"role": "user", "content": "初始化"}]},
        )
        assert first_path is not None
        real_atomic_write = prompt_audit_module._atomic_write_text

        def fail_markdown(path: Path, content: str) -> None:
            """只让请求 Markdown 写入失败，保留真实 sequence 分配过程。"""

            if path.suffix == ".md":
                raise OSError("audit disk failure")
            real_atomic_write(path, content)

        async def sdk_call() -> str:
            """返回可断言的真实 SDK 结果。"""

            nonlocal sdk_calls
            sdk_calls += 1
            return "sdk-result"

        monkeypatch.setattr(prompt_audit_module, "_atomic_write_text", fail_markdown)
        result, audit_path = await recorder.record_request_and_await(
            role="background_agent",
            source="background_work",
            protocol="claude_agent_sdk",
            model="claude-sonnet-4-5",
            operation="execute",
            request={"messages": [{"role": "user", "content": "任务"}]},
            trace_fields={"activity_id": "activity-a"},
            sdk_call=sdk_call,
        )

        assert result == "sdk-result"
        assert audit_path is None
        assert sdk_calls == 1

        # Markdown 失败发生在 sequence 分配后；恢复后下一条成功记录允许跳号。
        monkeypatch.setattr(
            prompt_audit_module,
            "_atomic_write_text",
            real_atomic_write,
        )
        _result, recovered_path = await recorder.record_request_and_await(
            role="background_agent",
            source="background_work",
            protocol="claude_agent_sdk",
            model="claude-sonnet-4-5",
            operation="execute",
            request={"messages": [{"role": "user", "content": "恢复请求"}]},
            trace_fields={"activity_id": "activity-b"},
            sdk_call=lambda: asyncio.sleep(0, result="recovered"),
        )
        assert recovered_path is not None
        assert recovered_path.name.startswith("000003__")

    asyncio.run(run())


def test_record_and_await_index_failure_keeps_markdown_and_calls_sdk_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引更新失败可以保留请求 Markdown，但不能重试真实 SDK。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=119)
        first_path = recorder.record_request(
            role="foreground",
            source="text_input",
            protocol="chat_completions",
            model="deepseek-flash",
            operation="initial",
            request={"messages": [{"role": "user", "content": "初始化"}]},
        )
        assert first_path is not None
        sdk_calls = 0
        real_atomic_write = prompt_audit_module._atomic_write_text

        def fail_index(path: Path, content: str) -> None:
            """请求 Markdown 正常落盘，只让 HTML 索引更新失败。"""

            if path.suffix == ".html":
                raise OSError("index disk failure")
            real_atomic_write(path, content)

        async def sdk_call() -> str:
            """记录索引失败后的真实 transport 调用次数。"""

            nonlocal sdk_calls
            sdk_calls += 1
            return "sdk-result"

        monkeypatch.setattr(prompt_audit_module, "_atomic_write_text", fail_index)
        result, audit_path = await recorder.record_request_and_await(
            role="background_agent",
            source="background_work",
            protocol="claude_agent_sdk",
            model="claude-sonnet-4-5",
            operation="execute",
            request={"messages": [{"role": "user", "content": "索引失败请求"}]},
            trace_fields={"activity_id": "activity-a"},
            sdk_call=sdk_call,
        )

        assert result == "sdk-result"
        assert audit_path is not None
        assert audit_path.exists()
        assert sdk_calls == 1

    asyncio.run(run())


def test_record_and_await_lock_failure_calls_sdk_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异步跨进程锁失败必须跳过审计，并且只执行一次 SDK query。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=116)
        sdk_calls = 0

        def fail_lock(_lock_file: object) -> None:
            """模拟跨进程锁超时。"""

            raise TimeoutError("lock busy")

        async def sdk_call() -> str:
            """记录 fail-open 后 SDK 的实际调用次数。"""

            nonlocal sdk_calls
            sdk_calls += 1
            return "sdk-result"

        monkeypatch.setattr(prompt_audit_module, "_lock_file", fail_lock)
        result, audit_path = await recorder.record_request_and_await(
            role="background_agent",
            source="background_work",
            protocol="claude_agent_sdk",
            model="claude-sonnet-4-5",
            operation="execute",
            request={"messages": [{"role": "user", "content": "任务"}]},
            trace_fields={"activity_id": "activity-a"},
            sdk_call=sdk_call,
        )

        assert result == "sdk-result"
        assert audit_path is None
        assert sdk_calls == 1
        assert not list(tmp_path.rglob("*.md"))

    asyncio.run(run())


def test_record_and_await_preserves_sdk_exception_and_releases_locks(
    tmp_path: Path,
) -> None:
    """SDK query 异常必须保持对象身份，并且不能卡住下一条提交。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=117)
        sdk_error = RuntimeError("transport write failed")

        async def failing_sdk_call() -> None:
            """抛出一份可按对象身份断言的 transport 异常。"""

            raise sdk_error

        with pytest.raises(RuntimeError) as captured:
            await recorder.record_request_and_await(
                role="background_agent",
                source="background_work",
                protocol="claude_agent_sdk",
                model="claude-sonnet-4-5",
                operation="execute",
                request={"messages": [{"role": "user", "content": "失败请求"}]},
                trace_fields={"activity_id": "activity-a"},
                sdk_call=failing_sdk_call,
            )
        assert captured.value is sdk_error

        async def succeeding_sdk_call() -> str:
            """第二条请求证明异常路径已经释放全部锁。"""

            return "recovered"

        result, _audit_path = await asyncio.wait_for(
            recorder.record_request_and_await(
                role="background_agent",
                source="background_work",
                protocol="claude_agent_sdk",
                model="claude-sonnet-4-5",
                operation="execute",
                request={"messages": [{"role": "user", "content": "后续请求"}]},
                trace_fields={"activity_id": "activity-b"},
                sdk_call=succeeding_sdk_call,
            ),
            timeout=1.0,
        )
        assert result == "recovered"

    asyncio.run(run())


def test_record_and_await_cancellation_releases_all_locks(tmp_path: Path) -> None:
    """SDK query 被取消后必须原样传播取消，并允许下一条请求继续提交。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=115)
        sdk_entered = asyncio.Event()
        release_sdk = asyncio.Event()

        async def blocking_sdk_call() -> None:
            """模拟已经进入 transport、但尚未写完的异步 query。"""

            sdk_entered.set()
            await release_sdk.wait()

        first_task = asyncio.create_task(
            recorder.record_request_and_await(
                role="background_agent",
                source="background_work",
                protocol="claude_agent_sdk",
                model="claude-sonnet-4-5",
                operation="execute",
                request={"messages": [{"role": "user", "content": "第一条"}]},
                trace_fields={"activity_id": "activity-a"},
                sdk_call=blocking_sdk_call,
            )
        )
        await sdk_entered.wait()
        first_task.cancel()
        await asyncio.sleep(0)
        assert first_task.done() is False
        release_sdk.set()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        async def succeeding_sdk_call() -> str:
            """第二条调用用于证明进程内锁和文件锁都已经释放。"""

            return "second-result"

        result, audit_path = await asyncio.wait_for(
            recorder.record_request_and_await(
                role="background_agent",
                source="background_work",
                protocol="claude_agent_sdk",
                model="claude-sonnet-4-5",
                operation="execute",
                request={"messages": [{"role": "user", "content": "第二条"}]},
                trace_fields={"activity_id": "activity-b"},
                sdk_call=succeeding_sdk_call,
            ),
            timeout=1.0,
        )

        assert result == "second-result"
        assert audit_path is not None

    asyncio.run(run())


def test_sync_and_async_recorders_share_one_sdk_submission_order(
    tmp_path: Path,
) -> None:
    """Chat 同步调用和 Background 异步 query 必须共享同一 sequence 顺序。"""

    async def run() -> None:
        recorder = _recorder(tmp_path, process_id=118)
        sdk_entry_order: list[str] = []
        markers = [f"mixed-{index:02d}" for index in range(12)]

        async def submit_async(marker: str) -> str:
            """模拟 Background Agent 的异步 transport 写入。"""

            async def sdk_call() -> str:
                sdk_entry_order.append(marker)
                await asyncio.sleep(0)
                return marker

            result, _audit_path = await recorder.record_request_and_await(
                role="background_agent",
                source="background_work",
                protocol="claude_agent_sdk",
                model="claude-sonnet-4-5",
                operation="execute",
                request={"messages": [{"role": "user", "content": marker}]},
                trace_fields={"llm_call_id": f"call-{marker}"},
                sdk_call=sdk_call,
            )
            return result

        def submit_sync(marker: str) -> str:
            """模拟 Chat Completions 的同步 SDK create 进入点。"""

            def sdk_call() -> str:
                sdk_entry_order.append(marker)
                return marker

            result, _audit_path = recorder.record_request_and_call(
                role="foreground",
                source="text_input",
                protocol="chat_completions",
                model="deepseek-flash",
                operation="initial",
                request={"messages": [{"role": "user", "content": marker}]},
                trace_fields={"llm_call_id": f"call-{marker}"},
                sdk_call=sdk_call,
            )
            return result

        tasks = []
        for index, marker in enumerate(markers):
            if index % 2 == 0:
                tasks.append(asyncio.create_task(submit_async(marker)))
            else:
                tasks.append(asyncio.create_task(asyncio.to_thread(submit_sync, marker)))
        results = await asyncio.gather(*tasks)

        assert sorted(results) == sorted(markers)
        markdown_paths = sorted(tmp_path.glob("assistant-runtime/runs/*/*.md"))
        audited_order = []
        for markdown_path in markdown_paths:
            markdown = markdown_path.read_text(encoding="utf-8")
            audited_order.append(next(marker for marker in markers if marker in markdown))
        assert audited_order == sdk_entry_order

    asyncio.run(run())


def _recorder(output_dir: Path, *, process_id: int) -> PromptAuditRecorder:
    """构造使用固定时钟的启用状态审查记录器。

    Args:
        output_dir: 测试专用的审查根目录。
        process_id: 写进运行目录的固定进程编号。

    Returns:
        可生成稳定文件路径的审查记录器。
    """

    return PromptAuditRecorder(
        enabled=True,
        output_dir=output_dir,
        max_runs=20,
        process_role="assistant-runtime",
        now=lambda: datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc),
        id_factory=lambda: "run-stable",
        process_id=process_id,
    )


def _streaming_client(
    create: Any,
    recorder: PromptAuditRecorder,
) -> ChatCompletionsClient:
    """构造带真实 Adapter 和可控 SDK 替身的流式客户端。

    Args:
        create: OpenAI SDK `create` 方法的测试替身。
        recorder: 本次测试需要检查的审查记录器。

    Returns:
        记录最终 SDK 请求的 Chat 客户端。
    """

    integration = get_model_integration("deepseek:deepseek-flash:chat")
    return ChatCompletionsClient(
        runtime_config=LlmRuntimeConfig(
            integration_id=integration.integration_id,
            provider_id=integration.provider_id,
            model=integration.model,
            base_url="https://api.example.test/v1",
            api_key="test-key",
        ),
        integration=integration,
        request_adapter=DeepSeekChatRequestAdapter(
            get_model_request_profile("deepseek-flash-chat-v1")
        ),
        output_policy=NoOpOutputPolicy(),
        role_name="foreground",
        prompt_audit_recorder=recorder,
        openai_client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )


def _chat_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
) -> object:
    """构造一个满足 Chat 流读取约定的最小响应分片。

    Args:
        content: 可选正文增量。
        finish_reason: 可选流结束原因。

    Returns:
        提供 choices/delta 字段的 SDK 替身。
    """

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=None),
                finish_reason=finish_reason,
            )
        ]
    )
