"""保存真实 ONNX 调用的输入缓存及输出，供固定历史回放。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import numpy as np
from environment_fingerprint import environment_fingerprint
from model_identity import model_identity


def install_trace() -> None:
    """按环境配置记录 session.run 调用；派生进程也会执行此入口。

    Returns:
        无；未配置输出目录时不改动 ONNX Runtime。
    """
    destination = os.environ.get("TORCH_FREE_TRACE")
    if not destination:
        return
    import onnxruntime as ort

    if getattr(ort.InferenceSession, "_torch_free_traced", False):
        return

    original = ort.InferenceSession
    # sitecustomize 早于 multiprocessing 设置进程名，使用 PID 避免父子进程覆盖同名文件。
    root = Path(destination) / f"process-{os.getpid()}-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    session_count = 0
    capture_all = os.environ.get("TORCH_FREE_TRACE_SAMPLED") != "1"

    class TracedSession(original):
        """原样执行推理，保存全部调用或显式选择的诊断样本。"""

        _torch_free_traced = True

        def __init__(self, path_or_bytes, *args, **kwargs):
            nonlocal session_count
            identity = model_identity(Path(path_or_bytes))
            # 旧 S2 默认配置自身不稳定；诊断基线显式记录此覆盖，绝不覆盖原始失败基线。
            deterministic_codec = os.environ.get(
                "TORCH_FREE_DETERMINISTIC_CODEC"
            ) == "1" and Path(str(path_or_bytes)).name.startswith("codec_")
            if deterministic_codec:
                if args:
                    raise RuntimeError("确定性诊断要求以关键字传入 session 配置")
                options = kwargs.get("sess_options") or ort.SessionOptions()
                options.use_deterministic_compute = True
                kwargs["sess_options"] = options
            super().__init__(path_or_bytes, *args, **kwargs)
            self.trace_dir = root / f"session-{session_count:03d}"
            self.trace_dir.mkdir(exist_ok=True)
            self.trace_index = 0
            session_count += 1
            meta = {
                "model": str(path_or_bytes),
                "providers": self.get_providers(),
                "model_sha256": identity["graph_sha256"],
                "model_identity": identity,
                "capture_mode": "all" if capture_all else "sampled",
                "deterministic_codec_override": deterministic_codec,
                "environment": environment_fingerprint(),
                "provider_options": self.get_provider_options(),
                "outputs": [item.name for item in self.get_outputs()],
                "input_types": {item.name: item.type for item in self.get_inputs()},
            }
            options = self.get_session_options()
            meta["options"] = {
                name: getattr(options, name)
                for name in (
                    "intra_op_num_threads",
                    "inter_op_num_threads",
                    "enable_cpu_mem_arena",
                    "enable_mem_pattern",
                    "use_deterministic_compute",
                )
            }
            meta["options"]["graph_optimization_level"] = int(
                options.graph_optimization_level
            )
            meta["options"]["execution_mode"] = int(options.execution_mode)
            (self.trace_dir / "session.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )

        def io_binding(self):
            binding = super().io_binding()
            feeds = {}
            output_names = []
            original_bind = binding.bind_cpu_input
            original_output = binding.bind_output

            def bind_cpu_input(name, array):
                feeds[name] = array
                return original_bind(name, array)

            binding.bind_cpu_input = bind_cpu_input

            def bind_output(name, *args, **kwargs):
                if name not in output_names:
                    output_names.append(name)
                return original_output(name, *args, **kwargs)

            def unsupported_binding(*args, **kwargs):
                raise RuntimeError(
                    "记录器仅支持 CPU 输入与按名称绑定的输出；不允许遗漏设备输入或输出"
                )

            binding.bind_output = bind_output
            binding.bind_input = unsupported_binding
            binding.bind_ortvalue_input = unsupported_binding
            binding.bind_ortvalue_output = unsupported_binding
            original_clear_inputs = binding.clear_binding_inputs
            original_clear_outputs = binding.clear_binding_outputs

            def clear_inputs():
                feeds.clear()
                return original_clear_inputs()

            def clear_outputs():
                output_names.clear()
                return original_clear_outputs()

            binding.clear_binding_inputs = clear_inputs
            binding.clear_binding_outputs = clear_outputs
            binding.trace_feeds = feeds
            binding.trace_output_names = output_names
            return binding

        def run_with_iobinding(self, binding, run_options=None):
            index = self.trace_index
            self.trace_index += 1
            capture = capture_all or index < 4 or index in {7, 15, 31}
            feeds = (
                {
                    key: np.asarray(value).copy()
                    for key, value in binding.trace_feeds.items()
                }
                if capture
                else {}
            )
            result = super().run_with_iobinding(binding, run_options)
            if capture:
                arrays = {"input:" + key: value for key, value in feeds.items()}
                values = binding.copy_outputs_to_cpu()
                arrays.update(
                    {
                        "output:" + name: np.asarray(value).copy()
                        for name, value in zip(
                            binding.trace_output_names, values, strict=True
                        )
                    }
                )
                np.savez(self.trace_dir / f"call-{index:04d}.npz", **arrays)
            return result

        def run(self, output_names, input_feed, run_options=None):
            # 保存进入计算前的缓存，防止运行结束后的原地更新污染基线。
            index = self.trace_index
            self.trace_index += 1
            capture = capture_all or index < 4 or index in {7, 15, 31}
            feeds = (
                {key: np.asarray(value).copy() for key, value in input_feed.items()}
                if capture
                else {}
            )
            result = super().run(output_names, input_feed, run_options)
            if capture:
                names = output_names or [item.name for item in self.get_outputs()]
                arrays = {"input:" + key: value for key, value in feeds.items()}
                arrays.update(
                    {
                        "output:" + name: np.asarray(value).copy()
                        for name, value in zip(names, result, strict=True)
                    }
                )
                np.savez(self.trace_dir / f"call-{index:04d}.npz", **arrays)
            return result

    ort.InferenceSession = TracedSession
