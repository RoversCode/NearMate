from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Literal, Sequence


LlmBackend = Literal["vulkan", "cuda", "cpu", "auto"]
ResolvedLlmBackend = Literal["vulkan", "cuda", "cpu"]
SUPPORTED_LLM_BACKENDS: tuple[LlmBackend, ...] = ("vulkan", "cuda", "cpu", "auto")


@dataclass(frozen=True)
class BackendSelectionSummary:
    """原生 llama.cpp 日志中的 backend 判定摘要。"""

    requested_backend: LlmBackend
    use_gpu: bool
    actual_backend: str | None
    is_success: bool
    failure_reason: str = ""
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class LlamaBackendResolution:
    """llama.cpp backend 自动选择结果。"""

    requested_backend: LlmBackend
    use_gpu: bool
    backend: ResolvedLlmBackend
    reason: str
    candidate_backends: tuple[ResolvedLlmBackend, ...]
    skipped_reasons: tuple[str, ...] = ()
    nvidia_gpu_names: tuple[str, ...] = ()


def detect_nvidia_gpu_names() -> tuple[str, ...]:
    """通过 nvidia-smi 尽力检测当前机器的 NVIDIA GPU 名称。"""

    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ()

    if completed.returncode != 0:
        return ()

    return tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip()
    )


def resolve_llama_backend_policy(
    bin_dir: Path,
    *,
    requested_backend: LlmBackend,
    nvidia_gpu_names: Sequence[str] | None = None,
) -> LlamaBackendResolution:
    """按硬件与 DLL 可用性解析 llama.cpp 的最终 backend。"""

    normalized_backend = validate_backend_policy(requested_backend)
    detected_nvidia_names = (
        tuple(nvidia_gpu_names)
        if nvidia_gpu_names is not None
        else detect_nvidia_gpu_names()
    )

    if normalized_backend != "auto":
        return LlamaBackendResolution(
            requested_backend=normalized_backend,
            use_gpu=normalized_backend != "cpu",
            backend=normalized_backend,
            reason=f"显式请求 {normalized_backend} backend",
            candidate_backends=(normalized_backend,),
            nvidia_gpu_names=detected_nvidia_names,
        )

    candidate_backends: tuple[ResolvedLlmBackend, ...] = (
        ("cuda", "vulkan", "cpu")
        if detected_nvidia_names
        else ("vulkan", "cpu")
    )
    skipped_reasons: list[str] = []
    for backend in candidate_backends:
        if backend == "cpu":
            return LlamaBackendResolution(
                requested_backend=normalized_backend,
                use_gpu=False,
                backend="cpu",
                reason="未找到可用 GPU backend DLL，回落 CPU",
                candidate_backends=candidate_backends,
                skipped_reasons=tuple(skipped_reasons),
                nvidia_gpu_names=detected_nvidia_names,
            )

        backend_path = _first_existing_backend_path(bin_dir, _gpu_backend_filenames(backend))
        if backend_path is None:
            skipped_reasons.append(f"{backend} backend DLL 不存在")
            continue

        return LlamaBackendResolution(
            requested_backend=normalized_backend,
            use_gpu=True,
            backend=backend,
            reason=f"选择 {backend_path.name}",
            candidate_backends=candidate_backends,
            skipped_reasons=tuple(skipped_reasons),
            nvidia_gpu_names=detected_nvidia_names,
        )

    return LlamaBackendResolution(
        requested_backend=normalized_backend,
        use_gpu=False,
        backend="cpu",
        reason="没有可用 backend 候选，回落 CPU",
        candidate_backends=candidate_backends,
        skipped_reasons=tuple(skipped_reasons),
        nvidia_gpu_names=detected_nvidia_names,
    )


def build_runtime_backend_load_paths(
    bin_dir: Path,
    *,
    use_gpu: bool,
    backend: LlmBackend,
) -> tuple[Path, ...]:
    """生成 llama.cpp 运行时应尝试装载的 backend 动态库路径序列。"""

    load_paths: list[Path] = []

    cpu_backend = _select_cpu_backend_path(bin_dir)
    if cpu_backend is not None:
        load_paths.append(cpu_backend)

    if not use_gpu:
        return tuple(load_paths)

    if backend == "vulkan":
        gpu_candidates = _gpu_backend_filenames("vulkan")
    elif backend == "cuda":
        gpu_candidates = _gpu_backend_filenames("cuda")
    else:
        gpu_candidates = [
            *_gpu_backend_filenames("cuda"),
            *_gpu_backend_filenames("vulkan"),
        ]

    for candidate in gpu_candidates:
        candidate_path = bin_dir / candidate
        if candidate_path.exists():
            load_paths.append(candidate_path)

    return tuple(load_paths)


def validate_backend_policy(value: str) -> LlmBackend:
    """校验并归一化 llm backend 配置。"""

    if value not in SUPPORTED_LLM_BACKENDS:
        raise ValueError(
            "llm_backend 只能是 vulkan、cuda、cpu 或 auto，"
            f"当前收到：{value}"
        )
    return value


def build_required_backend_dll_paths(
    bin_dir: Path,
    *,
    use_gpu: bool,
    backend: LlmBackend,
) -> list[Path]:
    """构造请求 backend 所依赖的最小 DLL 列表。"""

    required_paths = [
        bin_dir / "llama.dll",
        bin_dir / "ggml.dll",
        bin_dir / "ggml-base.dll",
    ]
    if not use_gpu:
        return required_paths
    if backend == "vulkan":
        return [*required_paths, bin_dir / "ggml-vulkan.dll"]
    if backend == "cuda":
        return [*required_paths, bin_dir / "ggml-cuda.dll"]
    return required_paths


def summarize_backend_selection(
    *,
    requested_backend: LlmBackend,
    use_gpu: bool,
    log_lines: Sequence[str],
) -> BackendSelectionSummary:
    """根据 llama.cpp 原生日志总结实际 backend 结果。"""

    relevant_lines = tuple(_collect_relevant_lines(log_lines))
    actual_backend = _detect_actual_backend(relevant_lines)

    if not use_gpu:
        return BackendSelectionSummary(
            requested_backend=requested_backend,
            use_gpu=use_gpu,
            actual_backend=actual_backend,
            is_success=True,
            evidence=relevant_lines,
        )

    if actual_backend not in {"vulkan", "cuda"}:
        return BackendSelectionSummary(
            requested_backend=requested_backend,
            use_gpu=use_gpu,
            actual_backend=actual_backend or "unknown",
            is_success=False,
            failure_reason="CPU fallback: llama.cpp 未成功进入 GPU backend",
            evidence=relevant_lines,
        )

    if requested_backend != "auto" and actual_backend != requested_backend:
        return BackendSelectionSummary(
            requested_backend=requested_backend,
            use_gpu=use_gpu,
            actual_backend=actual_backend,
            is_success=False,
            failure_reason=(
                "backend mismatch: "
                f"requested={requested_backend} actual={actual_backend}"
            ),
            evidence=relevant_lines,
        )

    return BackendSelectionSummary(
        requested_backend=requested_backend,
        use_gpu=use_gpu,
        actual_backend=actual_backend,
        is_success=True,
        evidence=relevant_lines,
    )


def _first_existing_backend_path(
    bin_dir: Path,
    candidates: Sequence[str],
) -> Path | None:
    """返回首个存在的 backend 动态库路径。"""

    for candidate in candidates:
        candidate_path = bin_dir / candidate
        if candidate_path.exists():
            return candidate_path
    return None


def _select_cpu_backend_path(bin_dir: Path) -> Path | None:
    """按平台选择最合适的 CPU backend 动态库。"""

    preferred = _first_existing_backend_path(bin_dir, _cpu_backend_filenames())
    if preferred is not None:
        return preferred

    if sys.platform == "win32":
        cpu_variants = sorted(bin_dir.glob("ggml-cpu-*.dll"))
        return cpu_variants[0] if cpu_variants else None
    if sys.platform == "darwin":
        return None

    cpu_variants = sorted(bin_dir.glob("libggml-cpu-*.so"))
    return cpu_variants[0] if cpu_variants else None


def _cpu_backend_filenames() -> tuple[str, ...]:
    """返回当前平台可接受的 CPU backend 文件名候选。"""

    if sys.platform == "win32":
        return (
            "ggml-cpu.dll",
            "ggml-cpu-x64.dll",
            "ggml-cpu-sse42.dll",
        )
    if sys.platform == "darwin":
        return ("libggml-cpu.dylib",)
    return (
        "libggml-cpu.so",
        "libggml-cpu-x64.so",
        "libggml-cpu-sse42.so",
    )


def _gpu_backend_filenames(backend: Literal["vulkan", "cuda"]) -> tuple[str, ...]:
    """返回当前平台对应 GPU backend 的文件名候选。"""

    if backend == "cuda":
        if sys.platform == "win32":
            return ("ggml-cuda.dll",)
        if sys.platform == "darwin":
            return ("libggml-cuda.dylib",)
        return ("libggml-cuda.so",)

    if sys.platform == "win32":
        return ("ggml-vulkan.dll",)
    if sys.platform == "darwin":
        return ("libggml-vulkan.dylib",)
    return ("libggml-vulkan.so",)


def _collect_relevant_lines(log_lines: Sequence[str]) -> list[str]:
    """提取 backend 判定需要的关键日志。"""

    keywords = (
        "using device",
        "assigned to device",
        "model buffer size",
        "kv buffer size",
        "compute buffer size",
        "dev = ",
        "backend_ptrs.size()",
    )
    return [
        line.strip()
        for line in log_lines
        if line and any(keyword in line.lower() for keyword in keywords)
    ]


def _detect_actual_backend(log_lines: Sequence[str]) -> str | None:
    """从关键日志中推断当前实际 backend。"""

    lowered_lines = [line.lower() for line in log_lines]

    if any("using device vulkan" in line for line in lowered_lines):
        return "vulkan"
    if any("using device cuda" in line for line in lowered_lines):
        return "cuda"

    has_vulkan = any(
        any(marker in line for marker in ("vulkan", "dev = vulkan"))
        for line in lowered_lines
    )
    has_cuda = any(
        any(marker in line for marker in ("cuda", "dev = cuda"))
        for line in lowered_lines
    )
    has_cpu = any(
        any(marker in line for marker in ("assigned to device cpu", "cpu kv buffer size", "cpu compute buffer size", "dev = cpu"))
        for line in lowered_lines
    )

    if has_vulkan and not has_cuda:
        return "vulkan"
    if has_cuda and not has_vulkan:
        return "cuda"
    if has_cpu and not has_vulkan and not has_cuda:
        return "cpu"
    if has_vulkan:
        return "vulkan"
    if has_cuda:
        return "cuda"
    if has_cpu:
        return "cpu"
    return None
