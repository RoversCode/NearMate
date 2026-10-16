"""提供 Windows 已安装应用发现、匹配和同步启动能力。"""

from companion.capabilities.windows_applications.models import (
    ApplicationLaunchResult,
    ApplicationRuntimeSnapshot,
    InstalledApplication,
    LaunchMode,
)
from companion.capabilities.windows_applications.service import (
    WindowsApplicationLaunchService,
    match_installed_applications,
    normalize_application_name,
)
from companion.capabilities.windows_applications.windows_gateway import (
    WindowsApplicationGateway,
)

__all__ = [
    "ApplicationLaunchResult",
    "ApplicationRuntimeSnapshot",
    "InstalledApplication",
    "LaunchMode",
    "WindowsApplicationGateway",
    "WindowsApplicationLaunchService",
    "match_installed_applications",
    "normalize_application_name",
]
