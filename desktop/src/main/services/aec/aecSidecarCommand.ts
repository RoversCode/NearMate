import { delimiter, join } from "node:path";
import type { AecSidecarSession } from "./aecSidecarTypes";

/**
 * buildAecSidecarSpawnArgs 生成 Python AEC sidecar 的启动参数。
 *
 * @param session 后端签发的 AEC 参与者会话。
 * @param parentPid 当前 Electron 主进程 PID。
 * @returns 可直接传给 child_process.spawn 的参数数组。
 */
export function buildAecSidecarSpawnArgs(
  session: AecSidecarSession,
  parentPid: number,
  packaged = false
): string[] {
  const args = [
    // 中文管道编码和禁写字节码通过参数明确设置，不依赖会被隔离模式忽略的环境变量。
    ...(packaged ? ["-I", "-B", "-X", "utf8", "-c", "from audio_aec_sidecar.main import main; main()"] : ["-B", "-X", "utf8", "-m", "audio_aec_sidecar.main"]),
    "--livekit-url",
    session.livekitUrl,
    "--token",
    session.token,
    "--room-name",
    session.roomName,
    "--identity",
    session.identity,
    "--parent-pid",
    String(parentPid),
    "--sample-rate",
    "48000",
    "--clean-output-sample-rate",
    "16000",
    "--warmup-seconds",
    "2",
    "--audio-device-mode",
    session.audioDeviceSelection.mode
  ];

  appendOptionalDeviceArg(args, "--microphone-id", session.audioDeviceSelection.microphoneId);
  appendOptionalDeviceArg(args, "--microphone-name", session.audioDeviceSelection.microphoneName);
  appendOptionalDeviceArg(args, "--speaker-loopback-id", session.audioDeviceSelection.speakerLoopbackId);
  appendOptionalDeviceArg(args, "--speaker-loopback-name", session.audioDeviceSelection.speakerLoopbackName);

  /**
   * 调试录音默认不落盘，只有后端明确开启时才追加输出目录和保留上限。
   */
  if (session.debugAudio.enabled) {
    args.push(
      "--debug-output-dir",
      session.debugAudio.outputDir,
      "--max-debug-runs",
      String(session.debugAudio.maxRuns)
    );
  }

  return args;
}

/**
 * buildAecSidecarSpawnEnv 生成 sidecar 子进程环境变量。
 *
 * @param repoRoot 当前仓库根目录。
 * @param baseEnv 主进程已有环境变量。
 * @returns 追加开发态 sidecar 源码路径后的环境变量。
 */
export function buildAecSidecarSpawnEnv(
  repoRoot: string,
  baseEnv: NodeJS.ProcessEnv
): NodeJS.ProcessEnv {
  const sidecarSourcePath = join(repoRoot, "tools", "audio-aec-poc", "src");

  return {
    ...baseEnv,
    PYTHONPATH: buildPythonPath(sidecarSourcePath, baseEnv["PYTHONPATH"])
  };
}

/**
 * buildPythonPath 组合 sidecar 专用 PYTHONPATH。
 *
 * @param sidecarSourcePath 必须优先加载的 sidecar 源码路径。
 * @param existingPythonPath 进程已有 PYTHONPATH。
 * @returns 可传给 Python 子进程的 PYTHONPATH。
 */
function buildPythonPath(
  sidecarSourcePath: string,
  existingPythonPath: string | undefined
): string {
  if (!existingPythonPath) {
    return sidecarSourcePath;
  }

  return [sidecarSourcePath, existingPythonPath].join(delimiter);
}

/**
 * appendOptionalDeviceArg 追加非空设备参数。
 *
 * @param args 正在构建的 CLI 参数数组。
 * @param flag 设备参数名。
 * @param value 后端下发的设备选择值。
 */
function appendOptionalDeviceArg(args: string[], flag: string, value: string | null): void {
  const normalizedValue = value?.trim();
  if (!normalizedValue) {
    return;
  }

  args.push(flag, normalizedValue);
}
