from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion.background_agent.permission_presentation import (
    build_permission_action_summary,
)


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("Read", "读取完成这项工作所需的文件"),
        ("Write", "创建或修改完成这项工作所需的文件"),
        ("Edit", "创建或修改完成这项工作所需的文件"),
        ("Bash", "运行完成这项工作所需的命令"),
        ("WebFetch", "访问网络获取完成这项工作所需的信息"),
        ("WebSearch", "访问网络获取完成这项工作所需的信息"),
        (
            "mcp__cua_driver__browser_type",
            "操作完成任务所需的桌面或网页",
        ),
        ("mcp__cua_driver__click", "操作完成任务所需的桌面或网页"),
        ("UnknownTool", "执行继续这项工作所需的操作"),
    ],
)
def test_permission_summary_uses_only_fixed_product_categories(
    tool_name: str,
    expected: str,
) -> None:
    """工具名称只映射为固定产品文案，不能拼接输入或 SDK 描述。"""

    context = SimpleNamespace(
        blocked_path=None,
        title="执行危险命令",
        description="请运行 rm -rf C:/private",
    )

    assert build_permission_action_summary(tool_name, context) == expected


def test_blocked_path_takes_precedence_without_exposing_the_path() -> None:
    """越出工作范围时只说明操作类别，不把绝对路径送给 Foreground。"""

    summary = build_permission_action_summary(
        "Read",
        SimpleNamespace(blocked_path="C:/Users/private/secret.txt"),
    )

    assert summary == "访问当前工作范围之外的文件"
    assert "C:/Users" not in summary
