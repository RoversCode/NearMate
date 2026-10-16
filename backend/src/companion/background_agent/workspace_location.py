"""解析后台智能体固定工作区所在的用户文档目录。"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from uuid import UUID


DEFAULT_WORKSPACE_NAME = "NearMate Workspace"
_WINDOWS_DOCUMENTS_FOLDER_ID = UUID("fdd39ad0-238f-46af-adb4-6c85480369c7")


def resolve_default_workspace_root() -> Path:
    """返回 V1 固定后台智能体工作区。

    Windows 用户可以把“文档”迁移到 OneDrive 或其它磁盘，因此不能把英文
    ``Documents`` 直接拼到用户主目录。非 Windows 平台保留主目录下的常规位置。

    Returns:
        用户实际文档目录下的 NearMate 工作区绝对路径。
    """

    documents_root = _resolve_user_documents_directory()
    return (documents_root / DEFAULT_WORKSPACE_NAME).resolve()


def _resolve_user_documents_directory() -> Path:
    """读取当前系统认可的用户文档目录。

    Returns:
        Windows Known Folder 或其它平台约定的文档目录。
    """

    if sys.platform != "win32":
        return Path.home() / "Documents"

    try:
        return _resolve_windows_documents_directory()
    except OSError:
        # Known Folder API 极少数情况下会因用户档案尚未就绪而失败；此时仍给出
        # 可预测的后备位置，避免整个助手子进程因为工作区路径无法启动。
        return Path.home() / "Documents"


def _resolve_windows_documents_directory() -> Path:
    """通过 Windows Known Folder API 读取“文档”目录。

    Returns:
        支持 OneDrive 和用户手动重定向的文档目录。

    Raises:
        OSError: Windows API 调用失败或没有返回路径。
    """

    folder_id_buffer = (ctypes.c_ubyte * 16).from_buffer_copy(
        _WINDOWS_DOCUMENTS_FOLDER_ID.bytes_le
    )
    path_pointer = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_id_buffer),
        0,
        None,
        ctypes.byref(path_pointer),
    )
    if result != 0 or not path_pointer.value:
        raise OSError(result, "Windows 无法解析用户文档目录")

    try:
        return Path(path_pointer.value)
    finally:
        ole32.CoTaskMemFree(ctypes.cast(path_pointer, ctypes.c_void_p))
