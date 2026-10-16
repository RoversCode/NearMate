from __future__ import annotations


class ApplicationError(Exception):
    """应用层可预期错误的基类。

    Args:
        detail: 可以安全返回给本地控制客户端的错误详情。
    """

    def __init__(self, detail: object) -> None:
        super().__init__(str(detail))
        self.detail = detail


class InvalidRequest(ApplicationError):
    """请求内容不满足业务校验规则。"""


class ResourceNotFound(ApplicationError):
    """请求引用的本地资源不存在。"""


class StateConflict(ApplicationError):
    """当前运行状态不允许执行请求动作。"""


class ServiceUnavailable(ApplicationError):
    """请求依赖的可选运行服务尚不可用。"""


class ApplicationOperationFailed(ApplicationError):
    """外部运行时或基础设施操作执行失败。"""
