from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class ConsoleAssetsMount:
    """挂载控制台构建产物并处理单页应用回退。

    Args:
        dist_dir: Console/Vite 构建输出目录。
    """

    def __init__(self, dist_dir: str) -> None:
        self.dist_path = Path(dist_dir)
        self.index_path = self.dist_path / "index.html"
        self.assets_path = self.dist_path / "assets"

    def mount(self, app: FastAPI) -> None:
        """在构建产物完整时注册静态资源和 SPA 路由。

        Args:
            app: 正在构建的控制服务应用。
        """

        # 控制台产物是可选部署资产，开发和 API 测试环境缺失时保持无操作。
        if not self.dist_path.is_dir() or not self.index_path.is_file():
            return

        app.add_api_route(
            "/console",
            self.console_index,
            methods=["GET"],
            include_in_schema=False,
        )
        if self.assets_path.is_dir():
            # Vite 哈希资源按真实文件提供，不能被 SPA 回退错误转换为入口 HTML。
            app.mount(
                "/console/assets",
                StaticFiles(directory=self.assets_path),
                name="console-assets",
            )
        else:
            app.add_api_route(
                "/console/assets/{asset_path:path}",
                self.console_missing_asset,
                methods=["GET"],
                include_in_schema=False,
            )
        app.add_api_route(
            "/console/{console_path:path}",
            self.console_spa_fallback,
            methods=["GET"],
            include_in_schema=False,
        )
        app.add_api_route(
            "/console/",
            self.console_spa_fallback,
            methods=["GET"],
            include_in_schema=False,
        )

    async def console_index(self) -> FileResponse:
        """返回控制台入口 HTML。"""

        return FileResponse(self.index_path)

    async def console_missing_asset(self, asset_path: str) -> None:
        """在 assets 目录缺失时返回静态资源 404。

        Args:
            asset_path: 浏览器请求的资源相对路径。
        """

        raise HTTPException(status_code=404, detail=f"console asset not found: {asset_path}")

    async def console_spa_fallback(self, console_path: str = "") -> FileResponse:
        """为任意非 assets 前端路径返回控制台入口 HTML。

        Args:
            console_path: 浏览器端负责解析的前端路由。

        Returns:
            已验证存在的控制台入口文件响应。
        """

        _ = console_path
        return FileResponse(self.index_path)


def mount_console_assets(app: FastAPI, dist_dir: str) -> None:
    """挂载本地控制台静态构建产物。

    Args:
        app: 正在构建的控制服务应用。
        dist_dir: Console/Vite 构建输出目录。
    """

    ConsoleAssetsMount(dist_dir).mount(app)
