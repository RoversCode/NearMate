from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class LiveKitTokenConfig:
    """LiveKit token 签发配置。"""

    api_key: str
    api_secret: str


class LiveKitTokenService:
    """LiveKit 入房 token 签发服务。"""

    def __init__(self, config: LiveKitTokenConfig) -> None:
        """初始化 token 签发服务。"""

        # API key 与 secret 来自 env.yaml 等配置源，进入服务边界后先去除首尾空白，避免配置误加空格导致签名不可用。
        api_key = config.api_key.strip()
        api_secret = config.api_secret.strip()
        if not api_key or not api_secret:
            raise ValueError("LiveKit API key and secret are required")

        # API secret 只保存在服务端内存中，用于 JWT 签名，不允许进入响应或日志。
        self._api_key = api_key
        self._api_secret = api_secret

    @staticmethod
    def create_participant_identity(prefix: str) -> str:
        """创建指定前缀的 LiveKit participant 身份。"""

        # 前缀为空时回退到 desktop，保证生成的 identity 对日志、LiveKit 控制台和诊断事件仍然可读。
        normalized_prefix = prefix.strip() or "desktop"
        return f"{normalized_prefix}-{uuid4().hex[:12]}"

    def create_join_token(self, room_name: str, identity: str, name: str) -> str:
        """签发指定房间的 join token。"""

        # room 与 identity 是 LiveKit 授权的最小追踪维度，任何一个为空都会让后续媒体调试不可定位。
        normalized_room = room_name.strip()
        normalized_identity = identity.strip()
        normalized_name = name.strip() or normalized_identity
        if not normalized_room or not normalized_identity:
            raise ValueError("LiveKit room name and identity are required")

        # LiveKit 使用 video grant 表达房间加入、发布和订阅权限；这里限定到单个 room，避免 token 被用于其它房间。
        issued_at = int(time.time())
        payload = {
            "iss": self._api_key,
            "sub": normalized_identity,
            "name": normalized_name,
            "nbf": issued_at,
            "exp": issued_at + 6 * 60 * 60,  # 6小时，token有效性
            "video": {
                "room": normalized_room,
                "roomJoin": True,
                "canPublish": True,
                "canSubscribe": True,
            },
        }

        return self._encode_jwt(payload)

    def _encode_jwt(self, payload: dict) -> str:
        """编码并签名 LiveKit JWT。"""

        # header 固定声明 HS256，与 LiveKit Server 对 API secret token 的校验方式一致。
        header_segment = _base64url_json({"alg": "HS256", "typ": "JWT"})
        payload_segment = _base64url_json(payload)
        signing_input = f"{header_segment}.{payload_segment}".encode("utf-8")

        # 使用 HMAC-SHA256 对 header.payload 签名，secret 只参与摘要计算，不会以明文出现在 token 中。
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        signature_segment = _base64url_bytes(signature)
        return f"{header_segment}.{payload_segment}.{signature_segment}"


def _base64url_json(value: dict) -> str:
    """将 JSON 对象编码成 JWT 使用的 base64url 段。"""

    # 使用紧凑分隔符减少 token 体积，并排序字段让测试与日志中的 payload 更稳定。
    raw_json = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _base64url_bytes(raw_json)


def _base64url_bytes(value: bytes) -> str:
    """将 bytes 编码成 JWT 使用的 base64url 字符串。"""

    # JWT 规范要求 base64url 字符串去掉等号补位，LiveKit Server 校验时会按同样规则还原。
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
