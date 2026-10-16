import type { SpawnSidecarProcess } from "./aecSidecarProcess";

/**
 * AEC sidecar 启动所需的最小 LiveKit 会话视图。
 */
export type AecSidecarSession = {
  livekitUrl: string;
  roomName: string;
  identity: string;
  token: string;
  debugAudio: {
    enabled: boolean;
    outputDir: string;
    maxRuns: number;
  };
  audioDeviceSelection: {
    mode: "system_default" | "manual";
    microphoneId: string | null;
    microphoneName: string | null;
    speakerLoopbackId: string | null;
    speakerLoopbackName: string | null;
  };
};

/**
 * AEC sidecar 生命周期对外暴露的脱敏诊断事件。
 */
export type AecSidecarDiagnostic =
  | {
      event: "aec-sidecar-exit";
      code: number | null;
      signal: NodeJS.Signals | null;
    }
  | {
      event: "aec-sidecar-error";
      phase: "start" | "runtime";
      message: string;
    }
  | {
      event: "aec-sidecar-warning";
      phase: "start" | "runtime";
      kind: "data_discontinuity" | "sidecar_restart_scheduled";
      message: string;
    }
  | {
      event: "aec-sidecar-output";
      phase: "start" | "runtime";
      message: string;
    };

/**
 * AEC sidecar supervisor 的固定依赖与测试注入点。
 */
export type CreateAecSidecarSupervisorConfig = {
  pythonExe: string;
  repoRoot: string;
  /** 安装版只传入主进程明确生成的环境；开发态保留已有环境。 */
  environment?: NodeJS.ProcessEnv;
  packaged?: boolean;
  parentPid: number;
  /** sidecar 启动后等待 ready 事件的最长时间；默认 30 秒。 */
  readyTimeoutMs?: number;
  /** 主动停止 sidecar 等待真实退出的最长时间；默认 8 秒。 */
  stopTimeoutMs?: number;
  spawn?: SpawnSidecarProcess;
  onDiagnostic?: (diagnostic: AecSidecarDiagnostic) => void;
};

/**
 * 主进程持有的 AEC sidecar 生命周期接口。
 */
export type AecSidecarSupervisor = {
  start(session: AecSidecarSession): Promise<void>;
  stop(): void;
  /** 立即终止当前及已登记的 sidecar，不等待 ready/exit Promise。 */
  forceStop(): void;
  stopAndWait?(): Promise<void>;
};
