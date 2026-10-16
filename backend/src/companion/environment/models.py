from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrozenEnvironmentSnapshot:
    """描述一次不可变的桌面现场快照。

    Args:
        snapshot_id: Backend 接纳快照后生成的唯一编号。
        captured_at: Desktop 实际取得屏幕帧的时间。
        active_window_title: 采集时的活动窗口标题。
        screenshot_mime_type: 截图 MIME；没有图片时为空。
        screenshot_bytes: 已压缩的截图字节；没有图片时为空。
        capture_error: 采集失败的稳定错误摘要。
        request_id: 触发语音采集的请求编号。
        turn_id: 快照所属语音 turn 编号。
        reason: 本次采集的业务原因。
    """

    snapshot_id: str
    captured_at: str
    active_window_title: str | None
    screenshot_mime_type: str | None
    screenshot_bytes: bytes | None
    capture_error: str | None
    request_id: str | None = None
    turn_id: str | None = None
    reason: str | None = None

    @property
    def screen_available(self) -> bool:
        """返回当前快照是否包含可供视觉理解的图片。"""

        return bool(self.screenshot_bytes and self.screenshot_mime_type)

    def to_payload(self) -> dict[str, object]:
        """转换为 prompt 与公开 API 可安全使用的元数据。"""

        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "active_window_title": self.active_window_title,
            "screen_available": self.screen_available,
            "capture_error": self.capture_error,
            "request_id": self.request_id,
            "turn_id": self.turn_id,
            "reason": self.reason,
        }
