"""用安装态私有 PATH 验证媒体 Skill 依赖和真实 FFmpeg 音频处理。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from companion.background_agent.skill_dependencies import SkillDependencyChecker
from companion.background_agent.skill_models import SkillDependencySpec


def verify_ffmpeg_runtime(runtime: Path, work: Path) -> dict[str, object]:
    """确认媒体工具来自安装包，并让三个内置媒体 Skill 通过同一依赖预检。

    Args:
        runtime: 解包后的 NearMate runtime 根目录。
        work: 本次 smoke 独占的可写临时目录。

    Returns:
        不含用户媒体的版本、Skill 状态和测试音频参数。

    Raises:
        RuntimeError: 工具来源、版本、依赖检查或实际媒体处理不符合发行约定。
    """
    runtime = runtime.resolve()
    binary_root = runtime / "tools" / "ffmpeg" / "bin"
    for command in ("ffmpeg", "ffprobe"):
        found = shutil.which(command)
        if found is None or Path(found).resolve() != binary_root / f"{command}.exe":
            raise RuntimeError(f"安装态私有 PATH 没有选中随包 {command}")
        result = subprocess.run(
            [command, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not result.stdout.startswith(f"{command} version 8.1.2-essentials_build-www.gyan.dev"):
            raise RuntimeError(f"安装态 {command} 版本不符合固定发行约定")

    checker = SkillDependencyChecker()
    ready_skills: list[str] = []
    skill_root = runtime / "backend" / "assets" / "agent-workspace" / ".claude" / "skills"
    for skill_id in ("bilibili-download", "ffmpeg-convert", "media-transcribe"):
        manifest = json.loads((skill_root / skill_id / "manifest.json").read_text(encoding="utf-8-sig"))
        dependencies = [SkillDependencySpec(**item) for item in manifest["dependencies"]]
        if not dependencies or any(not checker.check(item).ready for item in dependencies):
            raise RuntimeError(f"安装态媒体 Skill 依赖未就绪：{skill_id}")
        ready_skills.append(skill_id)

    # 文件存在和版本可读不能证明工具真的可用；生成一小段音频，再通过 ffprobe 读回参数。
    work = work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    audio_path = work / "ffmpeg-smoke.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.1", "-ar", "16000", "-ac", "1", str(audio_path)],
        capture_output=True, timeout=10, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(audio_path)],
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    streams = json.loads(result.stdout).get("streams", [])
    if len(streams) != 1 or streams[0].get("sample_rate") != "16000" or (
        streams[0].get("channels") != 1 or float(streams[0].get("duration", 0)) <= 0
    ):
        raise RuntimeError("安装态 FFmpeg 测试音频无法通过 ffprobe 验收")
    return {"version": "8.1.2", "ready_skills": ready_skills, "sample_rate": 16000,
            "channels": 1, "duration_seconds": float(streams[0]["duration"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("work", type=Path)
    arguments = parser.parse_args()
    print("NEARMATE_FFMPEG_PROBE=" + json.dumps(verify_ffmpeg_runtime(arguments.runtime, arguments.work)))
