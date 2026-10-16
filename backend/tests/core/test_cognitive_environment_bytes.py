from __future__ import annotations

from companion.foreground_tools.cognitive.contracts import (
    CognitiveExecutionPlan,
    CognitiveGoal,
)
from companion.foreground_tools.cognitive.executor import CognitiveTaskExecutor
from companion.materials.foreground_images import VisualEvidence


class RecordingCognitiveClient:
    """记录综合 Cognitive 请求的测试客户端。"""

    def __init__(self) -> None:
        """初始化最后一次请求记录。"""

        self.request: dict[str, object] | None = None

    def stream_text(self, **kwargs: object) -> tuple[str]:
        """记录请求并返回稳定文本流。

        Args:
            **kwargs: Cognitive 请求参数。

        Returns:
            单片段测试文本流。
        """

        self.request = dict(kwargs)
        return ("ok",)


def test_executor_sends_images_and_plan_specific_capabilities() -> None:
    """综合计划应同时进入消息图片和请求级联网推理策略。"""

    client = RecordingCognitiveClient()
    executor = CognitiveTaskExecutor(client=client, now=lambda: "2026-07-15T22:00:00+08:00")
    plan = CognitiveExecutionPlan(
        goals=(CognitiveGoal("lookup-a", "lookup_current_fact", "核查屏幕人物"),),
        reasoning="minimal",
        web_search=True,
        web_extract=False,
        images=(VisualEvidence("desktop", "桌面截图", "data:image/png;base64,environment"),),
        source_urls=(),
        progress_policy="reasoning_driven",
    )

    result = executor.stream(activity_id="cognitive-test", plan=plan, source_user_text="屏幕上的人是谁？")

    assert result == ("ok",)
    assert client.request is not None
    assert "data:image/png;base64,environment" in str(client.request["messages"])
    user_content = client.request["messages"][1]["content"]
    assert [block["type"] for block in user_content] == ["text", "text", "image_url"]
    assert "<desktop_view>" in user_content[1]["text"]
    system_prompt = str(client.request["messages"][0]["content"])
    assert "当前事实核查规则" in system_prompt
    assert "直接视觉读取规则" not in system_prompt
    intent = client.request["intent"]
    assert intent.output_format == "text"
    assert intent.thinking_level == "minimal"
    assert intent.web_search is True
    assert intent.web_extract is False
