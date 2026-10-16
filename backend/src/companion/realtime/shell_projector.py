from __future__ import annotations


class ShellProjector:
    """桌宠壳层事件投影器。"""

    def __init__(self, *, show_speech_bubbles: bool) -> None:
        """创建壳层投影器。

        Args:
            show_speech_bubbles: 是否为普通助手回复生成气泡。
        """

        self.show_speech_bubbles = show_speech_bubbles

    def project_assistant_reply(self, *, text: str) -> dict | None:
        """把助手最终回复投影为普通说话气泡。

        text 是助手整轮最终文本；当文本为空或 show_speech_bubbles 为 False 时返回 None。
        非空返回值是 shell.avatar_bubble_changed 事件，桌面端可按 ttl_ms 自动隐藏。
        """

        # 空文本没有可读信息，关闭开关时也不应打扰桌宠头像区域。
        if not self.show_speech_bubbles or not text:
            return None

        return {
            "type": "shell.avatar_bubble_changed",
            "payload": {
                "kind": "speech",
                "text": text,
                "ttl_ms": 3200,
            },
        }

