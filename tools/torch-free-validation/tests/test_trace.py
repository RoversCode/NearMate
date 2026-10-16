"""验证数值记录器不会遗漏绑定输出、混合运行记录或接受非有限数。"""

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pytest

onnx = pytest.importorskip("onnx")
from onnx import TensorProto, helper

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from numeric_metrics import compare_arrays


@pytest.mark.parametrize("value", [np.inf, -np.inf, np.nan])
def test_nonfinite_outputs_cannot_pass(value):
    """即便两边同为无穷，模型输出也不能标成数值通过。"""
    array = np.array([value], dtype=np.float32)
    assert not compare_arrays(array, array)["exact"]


def test_integer_and_dtype_must_match():
    """小整数和类型变化不能由浮点容差掩盖。"""
    assert not compare_arrays(np.array([1]), np.array([2]))["exact"]
    assert not compare_arrays(np.array([1], np.float32), np.array([1], np.float64))[
        "exact"
    ]


def test_binding_names_and_new_run_directories(tmp_path, monkeypatch):
    """输出逆序、清空绑定和新一轮运行都必须被记录器准确处理。"""
    trace = importlib.import_module("ort_trace")
    original_session = ort.InferenceSession
    model_path = tmp_path / "model.onnx"
    value = helper.make_tensor_value_info("x", TensorProto.FLOAT, [2])
    outputs = [
        helper.make_tensor_value_info(name, TensorProto.FLOAT, [2])
        for name in ("same", "negative")
    ]
    graph = helper.make_graph(
        [
            helper.make_node("Identity", ["x"], ["same"]),
            helper.make_node("Neg", ["x"], ["negative"]),
        ],
        "binding",
        [value],
        outputs,
    )
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=10
    )
    onnx.save(model, model_path)
    monkeypatch.setenv("TORCH_FREE_TRACE", str(tmp_path / "trace"))
    monkeypatch.setattr(trace, "environment_fingerprint", lambda: {"test": True})
    try:
        trace.install_trace()
        session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        binding = session.io_binding()
        data = np.array([2, 3], np.float32)
        binding.bind_cpu_input("x", data)
        binding.bind_output("negative")
        binding.bind_output("same")
        session.run_with_iobinding(binding)
        saved = np.load(session.trace_dir / "call-0000.npz")
        np.testing.assert_array_equal(saved["output:negative"], -data)
        np.testing.assert_array_equal(saved["output:same"], data)
        binding.clear_binding_outputs()
        binding.bind_output("same")
        session.run_with_iobinding(binding)
        assert np.load(session.trace_dir / "call-0001.npz").files == [
            "input:x",
            "output:same",
        ]
        with pytest.raises(RuntimeError, match="仅支持"):
            binding.bind_input("x", "cpu", 0, np.float32, [2], data.ctypes.data)
        metadata = json.loads((session.trace_dir / "session.json").read_text())
        assert metadata["capture_mode"] == "all"
        ort.InferenceSession = original_session
        trace.install_trace()
        next_session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        assert next_session.trace_dir != session.trace_dir
    finally:
        ort.InferenceSession = original_session
