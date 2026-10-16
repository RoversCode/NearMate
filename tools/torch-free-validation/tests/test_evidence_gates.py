"""阻止遗漏外部权重或改变计算环境后仍宣称回放通过。"""

import copy
import sys
from pathlib import Path

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
from onnx import helper, numpy_helper

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from environment_fingerprint import compare_environments
from model_identity import model_identity


def test_external_weights_are_part_of_identity(tmp_path):
    """只改变外部参数时，图摘要不变但模型身份必须改变。"""
    tensor = numpy_helper.from_array(np.array([1, 2], np.float32), "weight")
    graph = helper.make_graph([], "external", [], [], [tensor])
    model = helper.make_model(graph)
    path = tmp_path / "model.onnx"
    onnx.save_model(
        model,
        path,
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location="weights.data",
        size_threshold=0,
    )
    before = model_identity(path)
    (tmp_path / "weights.data").write_bytes(np.array([3, 4], np.float32).tobytes())
    after = model_identity(path)
    assert before["graph_sha256"] == after["graph_sha256"]
    assert before["external_weights"] != after["external_weights"]


def test_environment_requires_same_build_and_library_content():
    """安装目录及训练依赖可以不同，计算库内容和 ORT 构建必须相同。"""
    before = {
        "python": "3.12",
        "platform": "Windows",
        "ort_build": "same-build",
        "gpu": "same-device-driver",
        "packages": {"numpy": "2.4.6", "torch": "2.6.0"},
        "libraries": [
            {"path": "C:/baseline/onnxruntime.dll", "sha256": "same"},
            {"path": "C:/baseline/cublas64_12.dll", "sha256": "same"},
        ],
    }
    after = copy.deepcopy(before)
    after["packages"].pop("torch")
    after["libraries"][1]["path"] = "C:/candidate/nvidia/cublas64_12.dll"
    assert compare_environments(before, after, "CUDAExecutionProvider")["match"]
    after["libraries"][1]["sha256"] = "changed"
    assert not compare_environments(before, after, "CUDAExecutionProvider")["match"]
    after = copy.deepcopy(before)
    after["ort_build"] = "other-build"
    assert not compare_environments(before, after, "CUDAExecutionProvider")["match"]
    assert not compare_environments(None, before, "CPUExecutionProvider")["match"]
