from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from companion.core.application.errors import (
    ApplicationError,
    ApplicationOperationFailed,
    InvalidRequest,
    ResourceNotFound,
    ServiceUnavailable,
    StateConflict,
)


ERROR_STATUS_CODES: dict[type[ApplicationError], int] = {
    InvalidRequest: 400,
    ResourceNotFound: 404,
    StateConflict: 409,
    ApplicationOperationFailed: 500,
    ServiceUnavailable: 503,
}


async def application_error_handler(request: Request, exception: ApplicationError) -> JSONResponse:
    """把应用层错误映射为现有 FastAPI detail 响应。

    Args:
        request: 当前 HTTP 请求。
        exception: 应用服务抛出的可预期错误。

    Returns:
        保持既有错误载荷结构的 JSON 响应。
    """

    _ = request
    status_code = ERROR_STATUS_CODES.get(type(exception), 500)
    return JSONResponse(status_code=status_code, content={"detail": exception.detail})


def install_application_error_handlers(app: FastAPI) -> None:
    """给 FastAPI 注册应用错误统一映射。

    Args:
        app: 正在构建的控制服务应用。
    """

    app.add_exception_handler(ApplicationError, application_error_handler)
