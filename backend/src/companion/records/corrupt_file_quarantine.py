"""把已损坏的本地存储文件改名留证，让启动路径能按空内容安全重建。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


def quarantine_corrupt_file(path: Path, *, now: datetime | None = None) -> Path:
    """把损坏文件改名为带时间戳的备份，并把原路径腾空给重建流程。

    Args:
        path: 已确认损坏、永远无法再被正常解析的存储文件。
        now: 可选的当前时间；测试注入稳定时间戳，生产留空取本机时间。

    Returns:
        损坏文件改名后的备份路径。

    Raises:
        OSError: 改名失败时原样抛出，调用方继续按原损坏错误处理。
    """

    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.corrupt-{timestamp}")
    # 同一秒内可能隔离多份坏文件，追加序号保证每份证据都不被覆盖。
    sequence = 1
    while backup_path.exists():
        backup_path = path.with_name(f"{path.name}.corrupt-{timestamp}-{sequence}")
        sequence += 1
    os.replace(path, backup_path)
    return backup_path
