"""冻结模型版本与角色素材，防止确认试听时绑定到另一份内容。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from collections.abc import Callable

from companion.core.execution_config import ExecutionConfiguration, resolve_user_model_directory


MODEL_NAMES = {
    "qwen3-tts-clone-0.6b-gguf": "Qwen3-TTS 0.6B",
    "qwen3-tts-clone-1.7b-gguf": "Qwen3-TTS 1.7B",
    "s2-pro-4b-gguf": "S2-Pro",
    "voxcpm2-gguf": "VoxCPM2",
    "moss-tts-nano-onnx": "MOSS",
}


def sha256_file(path: Path) -> str:
    """分块计算文件摘要，不把大模型或音频整份放进内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_model_catalog(path: Path) -> dict[str, dict]:
    """读取发行固定的五个模型提交和文件摘要；缺少清单时禁止下载。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("models"), list):
        raise ValueError("模型发行清单格式错误，请修复安装资源")
    result = {}
    for item in payload["models"]:
        if not isinstance(item, dict):
            raise ValueError("模型清单条目必须是对象")
        model_id = item.get("model_id")
        if model_id not in MODEL_NAMES or model_id in result:
            raise ValueError("模型清单包含未知或重复模型")
        if not re.fullmatch(r"[0-9a-f]{40}", str(item.get("revision", ""))) or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("manifest_sha256", ""))):
            raise ValueError("模型清单缺少固定提交或摘要")
        if type(item.get("size_bytes")) is not int or item["size_bytes"] <= 0 or not isinstance(item.get("files"), list) or not item["files"]:
            raise ValueError("模型清单缺少下载大小或资产列表")
        seen = set()
        for entry in item["files"]:
            relative = entry.get("path", "") if isinstance(entry, dict) else ""
            path = Path(relative)
            if not relative or path.is_absolute() or path.drive or ".." in path.parts or relative in seen:
                raise ValueError("模型资产路径无效或重复")
            if type(entry.get("size_bytes")) is not int or entry["size_bytes"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                raise ValueError("模型资产缺少合法大小或摘要")
            seen.add(relative)
        result[model_id] = item
    return result


def validate_model_directory(directory: str, required_bytes: int = 0) -> Path:
    """确认用户选择的磁盘可写且能容纳下载缓存和模型暂存。

    Args:
        directory: 用户选择的模型根目录。
        required_bytes: 本次操作需要的额外空闲字节，包含缓存与暂存。
    Returns:
        已验证的绝对目录。
    """

    path = resolve_user_model_directory(directory)
    path.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=path) as stream:
            stream.write(b"nearmate-directory-check")
            stream.flush()
    except OSError as error:
        raise ValueError("模型目录不可写，请选择其他目录") from error
    required = required_bytes
    if shutil.disk_usage(path).free < required:
        raise ValueError(f"磁盘空间不足，需要至少 {required} 字节用于模型与暂存文件")
    return path


def published_model_matches(root: Path, model: dict, check_cancelled: Callable[[], None], report: Callable[[str], None]) -> bool:
    """逐块检查已发布模型；全部匹配时允许断网复用，不再复制一份权重。

    Args:
        root: 用户模型根目录。
        model: 安装资源固定的模型清单。
        check_cancelled: 每个读取块之前响应取消。
        report: 报告当前正在校验的文件名。
    Returns:
        原始模型清单及全部资产均符合发行摘要时为真。
    """

    directory = (root / model['model_id']).resolve()
    try:
        if sha256_file(directory / 'manifest.json') != model['manifest_sha256']:
            return False
        for item in model['files']:
            path = (directory / item['path']).resolve()
            if not path.is_relative_to(directory) or path.stat().st_size != item['size_bytes']:
                return False
            report(item['path'])
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while True:
                    check_cancelled()
                    block = stream.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            if digest.hexdigest() != item['sha256']:
                return False
        return True
    except OSError:
        return False


def inspect_model_installation(root: Path | None, model: dict, verification: dict | None = None) -> dict:
    """只读模型文件清单，区分文件已下载与曾经完成摘要校验。

    Args:
        root: 已确认的模型根目录；未确认时无法判断安装状态。
        model: 发行固定的模型、原始清单摘要与资产大小。
        verification: 本服务完成全文件校验时保存的文件记录。
    Returns:
        文件状态、现有大小与摘要校验资格；不会加载模型或修改下载目录。
    """

    result = {'local_state': 'unknown', 'installed_size_bytes': None,
              'total_size_bytes': model['size_bytes'], 'integrity_status': 'not_verified'}
    if root is None:
        return result
    directory = (root / model['model_id']).resolve()
    try:
        # 暂存和续传目录不算已下载模型，只有 SDK 原子发布的目录才能使用。
        if not directory.is_relative_to(root.resolve()):
            return {**result, 'local_state': 'incomplete'}
        directory.stat()
    except FileNotFoundError:
        return {**result, 'local_state': 'not_downloaded', 'installed_size_bytes': 0}
    except OSError:
        return result

    try:
        files = []
        complete = sha256_file(directory / 'manifest.json') == model['manifest_sha256']
        installed_bytes = 0
        for item in model['files']:
            path = (directory / item['path']).resolve()
            if not path.is_relative_to(directory):
                complete = False
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                complete = False
                continue
            installed_bytes += stat.st_size
            complete = complete and path.is_file() and stat.st_size == item['size_bytes']
            files.append({'path': item['path'], 'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
    except FileNotFoundError:
        return {**result, 'local_state': 'incomplete', 'installed_size_bytes': 0}
    except OSError:
        return result

    result.update(local_state='downloaded' if complete else 'incomplete', installed_size_bytes=installed_bytes)
    context = (verification or {}).get('context', {})
    # 大权重不能在每次打开设置时重新读几 GB。只有全摘要检查后的记录仍一致才公开 verified。
    if complete and verification and verification.get('verified_model_directory') == str(directory) and (
        context.get('model_revision') == model['revision']
        and context.get('manifest_sha256') == model['manifest_sha256']
        and verification.get('model_files') == files
    ):
        result['integrity_status'] = 'verified'
    return result


def model_download_space_required(root: Path, model: dict) -> int:
    """估算继续下载与原子发布需要的额外磁盘空间。

    Args:
        root: 用户模型根目录。
        model: 具有固定版本与资产大小的模型清单。
    Returns:
        完整发布副本、未缓存字节和小型工作文件所需的空间。
    """

    cache = root / '.streamvox-download-cache' / model['model_id'] / model['revision'] / model['manifest_sha256']
    remaining = 0
    for item in model['files']:
        path = cache / item['path']
        partial = path.with_name(path.name + '.part')
        # 缓存仍由下载器验证；这里只按已有占用估算增量，损坏文件重下时会给出实际磁盘错误。
        present = max((candidate.stat().st_size for candidate in (path, partial) if candidate.is_file()), default=0)
        remaining += max(0, item['size_bytes'] - min(present, item['size_bytes']))
    return model['size_bytes'] + remaining + 256 * 1024 * 1024


def voice_context(card, cards_root: Path, model: dict, configuration: ExecutionConfiguration) -> dict:
    """为一次试听冻结角色、音频、文本、模型版本和执行配置。"""

    binding = card.meta.voice_binding
    relative_audio = binding.reference_audio_path
    if not relative_audio or not (binding.prompt_text or "").strip():
        raise ValueError("当前角色卡缺少参考音频或对应文本，请补齐角色素材")
    card_root = (cards_root / card.meta.card_id).resolve()
    audio = (card_root / relative_audio).resolve()
    if not audio.is_relative_to(card_root) or not audio.is_file():
        raise ValueError("当前角色卡参考音频不存在或位于角色目录之外")
    context = {
        "card_id": card.meta.card_id,
        "role_name": binding.role_name.strip() or card.meta.card_id,
        "model_id": model["model_id"],
        "model_revision": model["revision"],
        "manifest_sha256": model["manifest_sha256"],
        "reference_audio_sha256": sha256_file(audio),
        "prompt_text": binding.prompt_text,
        "seed": binding.seed,
        "inference_options": dict(binding.inference_options),
        "execution": configuration.selection.model_dump(),
    }
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**context, "fingerprint": hashlib.sha256(encoded).hexdigest(), "reference_audio": str(audio)}
