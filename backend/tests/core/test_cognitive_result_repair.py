from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoal,
)
from companion.foreground_tools.cognitive.result_protocol import (
    CognitiveResultProtocolError,
)
from companion.foreground_tools.cognitive.result_repairer import (
    QwenFlashCognitiveResultRepairer,
    build_cognitive_repair_messages,
)
from companion.foreground_tools.cognitive.result_resolver import CognitiveResultResolver
from companion.llm.foreground_models import LlmTextStreamChunk
from companion.llm.request_intent import LlmInvocationIntent
from companion.realtime.cancellation import CancellationToken


@dataclass
class RecordingRepairer:
    """记录解析管线是否准确调用一次修复端口。"""

    response: str = '{"result":"修复结果","summary":"修复摘要"}'
    exception: BaseException | None = None

    def __post_init__(self) -> None:
        """初始化修复调用记录。"""

        self.calls: list[dict[str, object]] = []

    def repair(self, **kwargs: object) -> str:
        """保存调用参数并返回或抛出测试脚本指定结果。"""

        self.calls.append(dict(kwargs))
        if self.exception is not None:
            raise self.exception
        return self.response


class RecordingFlashClient:
    """记录 Qwen Flash 收到的消息和语义意图。"""

    def __init__(self, response: str) -> None:
        """保存准备按片段返回的正文。

        Args:
            response: 模拟 Flash 返回的完整文本。
        """

        self.response = response
        self.calls: list[dict[str, Any]] = []

    def stream_text(self, **kwargs: Any) -> list[LlmTextStreamChunk]:
        """记录请求并返回两段文本，验证修复器会完整拼接。"""

        self.calls.append(kwargs)
        midpoint = len(self.response) // 2
        return [
            LlmTextStreamChunk(kind="text_delta", text=self.response[:midpoint]),
            LlmTextStreamChunk(kind="text_delta", text=self.response[midpoint:]),
        ]


def _plan() -> CognitiveExecutionPlan:
    """构造修复测试使用的冻结目标。"""

    return CognitiveExecutionPlan(
        goals=(
            CognitiveGoal(
                tool_call_id="call-a",
                kind="lookup_current_fact",
                brief="核对当前版本",
                response_goal="给出准确版本",
            ),
        ),
        reasoning="high",
        web_search=True,
        web_extract=True,
        images=(),
        source_urls=("https://example.com",),
        progress_policy="reasoning_driven",
    )


def test_valid_plus_result_skips_flash() -> None:
    """Plus 结果已经合格时不能产生额外模型调用。"""

    repairer = RecordingRepairer()
    resolver = CognitiveResultResolver(repairer=repairer)

    result = resolver.resolve(
        activity_id="activity-a",
        plan=_plan(),
        raw_output="原结果\n<summary_tag>\n原摘要",
        cancellation_signal=None,
    )

    assert result.result == "原结果"
    assert repairer.calls == []


@pytest.mark.parametrize(
    "raw_output",
    [
        "not-json",
        "[]",
        '{"result":"结果","summary":null}',
        "<summary_tag>只有摘要",
        "只有正文<summary_tag>",
        "前缀<failure_tag>失败原因",
        "正文<summary_tag>摘要<summary_tag>重复摘要",
        "正文<summary_tag>摘要<failure_tag>失败原因",
    ],
)
def test_each_repairable_error_calls_flash_exactly_once(raw_output: str) -> None:
    """所有可修复协议错误都只能调用一次 Flash。

    Args:
        raw_output: 一种 Plus 协议错误正文。
    """

    repairer = RecordingRepairer()
    resolver = CognitiveResultResolver(repairer=repairer)

    result = resolver.resolve(
        activity_id="activity-a",
        plan=_plan(),
        raw_output=raw_output,
        cancellation_signal=None,
    )

    assert result.succeeded is True
    assert len(repairer.calls) == 1


def test_repair_can_produce_a_valid_business_failure() -> None:
    """Flash 只修格式后仍可保留 Plus 已表达的业务失败事实。"""

    repairer = RecordingRepairer(
        response=json.dumps(
            {
                "failure_reason": "必要来源无法读取。",
            },
            ensure_ascii=False,
        )
    )

    result = CognitiveResultResolver(repairer=repairer).resolve(
        activity_id="activity-a",
        plan=_plan(),
        raw_output="必要来源无法读取，任务没有完成。",
        cancellation_signal=None,
    )

    assert result.succeeded is False
    assert result.failure_reason == "必要来源无法读取。"


@pytest.mark.parametrize(
    "repairer",
    [
        RecordingRepairer(exception=RuntimeError("provider body: secret-raw")),
        RecordingRepairer(response=""),
        RecordingRepairer(response='{"result":"仍然缺字段"}'),
    ],
)
def test_flash_failure_or_second_invalid_result_becomes_safe_protocol_failure(
    repairer: RecordingRepairer,
) -> None:
    """Flash 异常、空响应和二次不合格都收口为不含原文的最终协议故障。

    Args:
        repairer: 模拟一种最终修复失败的端口。
    """

    secret_raw = "secret-plus-raw"
    with pytest.raises(CognitiveResultProtocolError) as captured:
        CognitiveResultResolver(repairer=repairer).resolve(
            activity_id="activity-a",
            plan=_plan(),
            raw_output=secret_raw,
            cancellation_signal=None,
        )

    assert captured.value.repairable is False
    assert secret_raw not in str(captured.value)
    assert "secret-raw" not in str(captured.value)
    assert len(repairer.calls) == 1


def test_blank_plus_output_never_calls_flash() -> None:
    """空白主正文直接失败，不复制给第二个模型。"""

    repairer = RecordingRepairer()
    for raw_output in ("", " "):
        with pytest.raises(CognitiveResultProtocolError):
            CognitiveResultResolver(repairer=repairer).resolve(
                activity_id="activity-a",
                plan=_plan(),
                raw_output=raw_output,
                cancellation_signal=None,
            )

    assert repairer.calls == []


def test_oversized_plus_output_never_calls_flash() -> None:
    """超长主正文直接失败，不复制给第二个模型。"""

    repairer = RecordingRepairer()
    with pytest.raises(CognitiveResultProtocolError):
        CognitiveResultResolver(repairer=repairer).resolve(
            activity_id="activity-a",
            plan=_plan(),
            raw_output="x" * 500_001,
            cancellation_signal=None,
        )

    assert repairer.calls == []


def test_qwen_flash_repairer_uses_non_thinking_json_without_other_capabilities() -> None:
    """Flash 修复固定关闭思考，且不请求工具、联网、网页读取或输出上限。"""

    response = '{"result":"修复结果","summary":"修复摘要"}'
    client = RecordingFlashClient(response)
    cancellation_token = CancellationToken()

    actual = QwenFlashCognitiveResultRepairer(client=client).repair(
        activity_id="activity-a",
        plan=_plan(),
        raw_output="原始错误正文",
        cancellation_signal=cancellation_token,
    )

    assert actual == response
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["intent"] == LlmInvocationIntent.json_object(thinking_level="off")
    assert call["cancellation_event"] is cancellation_token
    assert call["audit_source"] == "activity"
    assert call["audit_operation"] == "repair"
    assert "原始错误正文" in call["messages"][1]["content"]
    assert "核对当前版本" in call["messages"][1]["content"]
    assert "audit_messages_factory" not in call


def test_repair_messages_explain_plain_task_and_model_output_without_internal_terms() -> None:
    """Flash 只看到自解释的任务说明和原模型输出，不依赖项目内部术语。"""

    messages = build_cognitive_repair_messages(
        plan=_plan(),
        raw_output="原始错误正文",
    )

    system_prompt = messages[0]["content"]
    assert "另一个模型" in system_prompt
    assert "Cognitive" not in system_prompt
    assert "Foreground" not in system_prompt
    assert "task_goals" not in system_prompt
    assert "raw_output" not in system_prompt
    assert "<summary_tag>" in system_prompt
    assert "完整完成" in system_prompt
    assert "summary 必须只用一到两句话" in system_prompt
    assert "failure_reason 必须只用一到两句话" in system_prompt

    repair_input = json.loads(messages[1]["content"])
    assert repair_input == {
        "task_description": (
            "1. 核对当前版本\n"
            "   结果需要帮助完成：给出准确版本"
        ),
        "model_output": "原始错误正文",
    }
    assert "lookup_current_fact" not in messages[1]["content"]
    assert "call-a" not in messages[1]["content"]
