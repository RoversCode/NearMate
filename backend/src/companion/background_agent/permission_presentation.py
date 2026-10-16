"""把 SDK 权限请求压缩成 Foreground 可以安全转述的操作类别。"""

from __future__ import annotations


def build_permission_action_summary(tool_name: str, context: object) -> str:
    """生成不含命令、参数、路径和供应商描述的中文操作摘要。

    Desktop 仍从原始 ``tool_name`` 与 ``input_data`` 展示精确操作；本函数只服务
    Foreground 的语音和聊天表达，因此只允许返回固定产品文案。

    Args:
        tool_name: SDK 请求调用的工具名称。
        context: SDK 权限回调上下文，只检查是否存在 ``blocked_path``。

    Returns:
        Foreground 可以直接理解和转述的固定中文操作类别。
    """

    if _has_blocked_path(context):
        return "访问当前工作范围之外的文件"

    normalized_tool_name = tool_name.strip().casefold()
    if normalized_tool_name == "read":
        return "读取完成这项工作所需的文件"
    if normalized_tool_name in {"write", "edit", "notebookedit"}:
        return "创建或修改完成这项工作所需的文件"
    if normalized_tool_name == "bash":
        return "运行完成这项工作所需的命令"
    if normalized_tool_name in {"webfetch", "websearch", "search"}:
        return "访问网络获取完成这项工作所需的信息"
    if normalized_tool_name.startswith("mcp__cua_driver__"):
        return "操作完成任务所需的桌面或网页"
    return "执行继续这项工作所需的操作"


def _has_blocked_path(context: object) -> bool:
    """判断权限上下文是否声明了越出当前工作范围的路径。

    Args:
        context: SDK 对象或测试使用的字典替身。

    Returns:
        ``blocked_path`` 是非空字符串时返回真。
    """

    blocked_path = (
        context.get("blocked_path")
        if isinstance(context, dict)
        else getattr(context, "blocked_path", None)
    )
    return isinstance(blocked_path, str) and bool(blocked_path.strip())
