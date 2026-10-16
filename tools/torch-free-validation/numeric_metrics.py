"""严格区分结构错误、绝对误差、相对误差与浮点距离。"""

import numpy as np


def compare_arrays(actual: np.ndarray, expected: np.ndarray) -> dict:
    """比较数组，结构或整数差异不使用浮点容差。

    Args:
        actual: 候选环境的输出。
        expected: 保存的基线输出。
    Returns:
        结构一致性、精确一致性和可解释的数值指标。
    """
    result = {
        "shape_equal": actual.shape == expected.shape,
        "dtype_equal": actual.dtype == expected.dtype,
    }
    if not all(result.values()):
        return result | {
            "exact": False,
            "actual_shape": list(actual.shape),
            "expected_shape": list(expected.shape),
            "actual_dtype": str(actual.dtype),
            "expected_dtype": str(expected.dtype),
        }
    result["exact"] = bool(np.array_equal(actual, expected))
    if not np.issubdtype(actual.dtype, np.floating):
        return result
    result["finite"] = bool(np.isfinite(actual).all() and np.isfinite(expected).all())
    if not result["finite"]:
        return result | {"exact": False}
    difference = np.abs(actual.astype(np.float64) - expected.astype(np.float64))
    result["max_abs"] = float(difference.max(initial=0))
    usable = np.abs(expected.astype(np.float64)) >= np.finfo(expected.dtype).tiny
    result["max_relative_nonzero"] = float(
        np.max(
            difference[usable] / np.abs(expected[usable].astype(np.float64)), initial=0
        )
    )
    if actual.dtype in (np.dtype("float16"), np.dtype("float32")):
        unsigned = np.uint16 if actual.dtype.itemsize == 2 else np.uint32
        sign = 1 << (actual.dtype.itemsize * 8 - 1)
        # 负数位序相反；映射到单调整数后，跨零也能计算距离。
        first = actual.view(unsigned).astype(np.int64)
        second = expected.view(unsigned).astype(np.int64)
        first = np.where(first & sign, sign - (first & (sign - 1)), sign + first)
        second = np.where(second & sign, sign - (second & (sign - 1)), sign + second)
        result["max_ulp"] = int(np.max(np.abs(first - second), initial=0))
    return result
