from __future__ import annotations

import sys
from pathlib import Path

# 直接以脚本方式运行时，Python 只会把当前文件目录放进 sys.path，
# 这会导致 `from companion...` 无法解析到 backend/src 这个包根目录。。
CURRENT_FILE = Path(__file__).resolve()
SRC_ROOT = CURRENT_FILE.parents[2]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from companion.core.native_runtime_env import configure_native_runtime_environment


def run() -> None:
    """启动后端控制服务。"""

    # 控制服务入口统一配置 companion 命名空间日志，业务模块只需要通过统一 logger 取实例。
    configure_native_runtime_environment()
    # 先登记安装包原生库目录，再导入会触发音频和数值扩展的应用模块。
    import uvicorn
    from companion.core.app import build_app
    from companion.core.dev_ports import ensure_port_available
    from companion.core.settings import Settings
    from companion.observability.logging_config import configure_backend_logging, shutdown_backend_logging

    configure_backend_logging(process_role="backend-main")
    try:
        settings = Settings()
        try:
            ensure_port_available(settings.control_host, settings.control_port)
        except OSError as error:
            print(
                f"[backend] 控制端口 {settings.control_host}:{settings.control_port} 无法绑定，可能已被其它进程占用：{error}",
                file=sys.stderr,
            )
            raise SystemExit(1) from error

        uvicorn.run(build_app(), host=settings.control_host, port=settings.control_port)
    finally:
        shutdown_backend_logging()


if __name__ == "__main__":
    run()
