"""记录 ONNX 图及其外部权重，避免只核对没有参数的图文件。"""

from pathlib import Path

import onnx
from environment_fingerprint import file_sha256


def model_identity(path: Path) -> dict:
    """在加载模型前读取所有权重摘要，包括子图和稀疏张量。

    Args:
        path: ONNX 图文件路径。
    Returns:
        图摘要及按相对路径排序的外部权重摘要。
    """
    path = path.resolve()
    graph_hash = file_sha256(path)
    model = onnx.load(path, load_external_data=False)
    pending = [model]
    locations = set()
    while pending:
        message = pending.pop()
        if isinstance(message, onnx.TensorProto):
            if message.data_location == onnx.TensorProto.EXTERNAL:
                entries = {entry.key: entry.value for entry in message.external_data}
                location = entries.get("location")
                if not location:
                    raise ValueError("外部权重缺少文件位置，无法核对模型身份")
                locations.add(location)
            continue
        # 参数也可能藏在子图、函数或稀疏张量里，遍历消息字段而非只看顶层 initializer。
        for field, value in message.ListFields():
            if field.message_type is None:
                continue
            if field.is_repeated:
                pending.extend(value)
            else:
                pending.append(value)
    weights = {}
    for location in sorted(locations):
        weight_path = (path.parent / location).resolve()
        if not weight_path.is_relative_to(path.parent):
            raise ValueError(f"外部权重越过模型目录：{location}")
        weights[location] = file_sha256(weight_path)
    if graph_hash != file_sha256(path):
        raise RuntimeError("读取摘要期间模型图发生变化")
    return {"graph_sha256": graph_hash, "external_weights": weights}
