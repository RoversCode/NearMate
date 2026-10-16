from __future__ import annotations

import argparse
from pathlib import Path

from audio_aec_poc.pipeline import PipelineConfig, run_capture_pipeline


# 默认输出目录固定指向 PoC 工具目录，保证从仓库根目录执行时产物落在可预期位置。
DEFAULT_OUTPUT_DIR = Path("tools/audio-aec-poc/output")


def build_parser() -> argparse.ArgumentParser:
    """构建 Windows AEC PoC 的命令行参数解析器。

    功能：定义采集时长、输出目录、AEC 内部采样率、clean 输出采样率和 AEC 预热时长参数，供 main 解析后组装 PipelineConfig。
    核心入参：无显式入参，默认值来自当前 PoC 运行约定。
    预期输出：返回配置完毕的 argparse.ArgumentParser 实例。
    边界异常：本函数只声明参数，不主动抛出业务异常；非法参数类型由 argparse 在解析阶段处理。
    """
    # 解析器说明保持简短，避免 --help 输出干扰用户定位关键运行参数。
    parser = argparse.ArgumentParser(
        prog="audio-aec-poc",
        description="运行 Windows 扬声器 loopback reference AEC 采集验证。",
    )

    # 采集时长允许小数，便于开发阶段用很短时间快速验证管线和文件落盘。
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=10.0,
        help="采集持续秒数，默认 10.0。",
    )
    # 输出目录作为所有运行目录的父目录，真实运行会在其下创建带时间戳的子目录。
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="输出父目录，默认 tools/audio-aec-poc/output。",
    )
    # 采样率默认 48kHz，更贴近 Windows/WebRTC 常见音频管线，后续接 ASR 时再单独降采样。
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        help="AEC 内部采样率，默认 48000。",
    )
    # clean 输出采样率独立于 AEC 内部采样率，方便比较 48k AEC + 16k ASR 输入这类产品链路。
    parser.add_argument(
        "--clean-output-sample-rate",
        type=int,
        default=16000,
        help="mic_clean.wav 保存采样率，默认 16000。",
    )
    # 预热时长让 AEC 先学习当前设备和房间的回声路径，再开始写入正式试听文件。
    parser.add_argument(
        "--warmup-seconds",
        type=float,
        default=2.0,
        help="正式录制前只喂给 AEC 的预热秒数，默认 2.0。",
    )
    return parser


def main() -> None:
    """解析命令行参数并运行一次 Windows AEC PoC 采集管线。

    功能：读取 CLI 参数，创建单声道 PipelineConfig，调用 run_capture_pipeline，并打印运行目录和处理帧数。
    核心入参：通过命令行传入 duration_seconds、output_dir、sample_rate、clean_output_sample_rate 和 warmup_seconds。
    预期输出：标准输出展示本次运行的输出目录和成功处理的 10ms 帧数量。
    边界异常：参数类型错误由 argparse 处理；配置非法、设备不可用或文件写入失败会透传底层异常，便于 PoC 阶段直接诊断。
    """
    # CLI 层只负责解析用户输入，业务约束交给 PipelineConfig.validate 和管线内部统一执行。
    parser = build_parser()
    args = parser.parse_args()

    # 当前 Task 6 明确固定单声道，避免 CLI 提前暴露尚未验证的多声道行为。
    config = PipelineConfig(
        duration_seconds=args.duration_seconds,
        output_dir=args.output_dir,
        sample_rate=args.sample_rate,
        clean_output_sample_rate=args.clean_output_sample_rate,
        channels=1,
        warmup_seconds=args.warmup_seconds,
    )

    # 采集管线返回运行摘要，CLI 只打印用户最需要定位的目录和处理量。
    result = run_capture_pipeline(config)
    print(f"Output directory: {result.run_dir}")
    print(f"Frames processed: {result.frames_processed}")


# 支持 python -m audio_aec_poc.main 直接运行，匹配 README 中基于仓库根目录 .venv 的命令。
if __name__ == "__main__":
    main()
