"""用真实 Python 子进程检查设备探测和派生进程的只读运行参数。"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from companion.core import execution_probe


def test_probe_child_disables_bytecode_and_uses_utf8(tmp_path: Path, monkeypatch) -> None:
    """探测子进程导入模块时不能在安装目录生成字节码缓存。

    Args:
        tmp_path: 模拟随包模块的临时目录。
        monkeypatch: 只替换原生运算主体，保留生产启动器传给真实 Python 的参数。

    Returns:
        无；核对真实子进程参数和磁盘上没有出现缓存。
    """
    (tmp_path / 'probe_fixture.py').write_text('VALUE = 7\n', encoding='utf-8')
    original_run = subprocess.run
    child_code = (
        'import sys, json; from pathlib import Path; '
        f'sys.path.insert(0, {str(tmp_path)!r}); import probe_fixture; '
        'Path(sys.argv[-1]).write_text(json.dumps({"available": True, '
        '"dont_write_bytecode": sys.dont_write_bytecode, "isolated": sys.flags.isolated, '
        '"utf8_mode": sys.flags.utf8_mode, "value": probe_fixture.VALUE}), encoding="utf-8")'
    )

    def run_probe_body(command, **kwargs):
        """在隔离目录运行无硬件依赖的探测主体，命令行开关保持原样。

        Args:
            command: 生产探测器生成的完整启动命令。
            kwargs: 超时、输出采集和窗口可见性参数。

        Returns:
            真实子进程退出结果。
        """
        arguments = list(command)
        arguments[arguments.index('-c') + 1] = child_code
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(execution_probe.subprocess, 'run', run_probe_body)
    result = execution_probe.probe_backend('onnx', 'cpu')

    assert result['available'] is True
    assert result['value'] == 7
    assert result['dont_write_bytecode'] is True, {'flags': result, 'bytecode': list(tmp_path.rglob('*.pyc'))}
    assert result['isolated'] == 1
    assert result['utf8_mode'] == 1
    assert list(tmp_path.rglob('*.pyc')) == []


def test_spawn_inherits_read_only_interpreter_flags(tmp_path: Path) -> None:
    """正式助手、语音准备和 ASR 使用的派生方式必须继承父进程只读参数。

    Args:
        tmp_path: 用于运行真实派生进程的隔离脚本目录。

    Returns:
        无；核对子进程参数和导入模块后没有缓存。
    """
    (tmp_path / 'spawn_fixture.py').write_text('VALUE = 9\n', encoding='utf-8')
    driver = tmp_path / 'spawn_driver.py'
    driver.write_text('''import json
import multiprocessing
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent))
import spawn_fixture

def report_flags(connection):
    connection.send({"dont_write_bytecode": sys.dont_write_bytecode,
        "isolated": sys.flags.isolated, "utf8_mode": sys.flags.utf8_mode, "value": spawn_fixture.VALUE})
    connection.close()

if __name__ == "__main__":
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=report_flags, args=(sender,))
    process.start()
    sender.close()
    if not receiver.poll(10):
        process.terminate()
        raise RuntimeError("派生进程未返回参数")
    result = receiver.recv()
    process.join(10)
    assert process.exitcode == 0
    print(json.dumps(result))
''', encoding='utf-8')
    completed = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(driver)],
        capture_output=True, text=True, encoding='utf-8', timeout=20, check=True)

    assert json.loads(completed.stdout) == {'dont_write_bytecode': True, 'isolated': 1, 'utf8_mode': 1, 'value': 9}
    assert list(tmp_path.rglob('*.pyc')) == []


def test_cuda_probe_reports_real_cuda_execution_when_gpu_runtime_is_installed() -> None:
    """CUDA 包已经可导入时，设备探测必须证明算子确实落到 CUDA。"""

    try:
        import onnxruntime as ort
    except ImportError:
        pytest.skip("当前环境没有安装 ONNX Runtime")
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        pytest.skip("当前环境没有安装 CUDA ONNX Runtime")

    result = execution_probe.probe_backend("onnx", "cuda")

    assert result["available"] is True, result
    assert result["actual_providers"] == ["CUDAExecutionProvider"]
