type Live2dLipSyncAudioSourceNode = {
  connect(destination: unknown): unknown;
  disconnect(): void;
};

type Live2dLipSyncAnalyserNode = {
  fftSize: number;
  minDecibels: number;
  maxDecibels: number;
  smoothingTimeConstant: number;
  connect(destination: unknown): unknown;
  disconnect(): void;
  getFloatTimeDomainData(buffer: Float32Array): void;
};

export type Live2dLipSyncAudioContext = {
  destination: unknown;
  createAnalyser(): Live2dLipSyncAnalyserNode;
  createMediaElementSource(element: HTMLMediaElement): Live2dLipSyncAudioSourceNode;
  createMediaStreamSource?(stream: MediaStream): Live2dLipSyncAudioSourceNode;
  close?(): Promise<void>;
  resume?(): Promise<void>;
  state?: AudioContextState;
};

export type Live2dLipSyncInternalModel = {
  coreModel?: unknown;
  getIdSafe?: unknown;
  motionManager?: unknown;
  off?: unknown;
  on?: unknown;
  options?: unknown;
};

export type Live2dLipSyncWriterKind = "cubism" | "legacy" | "none";

export type Live2dLipSyncWriteResult =
  | {
      writerKind: "cubism" | "legacy";
      written: true;
    }
  | {
      reason: "missing_internal_model" | "missing_mouth_parameter_writer";
      writerKind: "none";
      written: false;
    };

export type Live2dLipSyncModel = {
  internalModel?: Live2dLipSyncInternalModel;
};

export type Live2dLipSyncTicker = {
  add(callback: () => void): void;
  remove(callback: () => void): void;
};

export type Live2dLipSyncModelBinding = {
  model: Live2dLipSyncModel;
  ticker: Live2dLipSyncTicker;
  /** 已实际提交到舞台的模型路径，不使用尚未完成切换的目标路径。 */
  modelPath?: string;
};

export type Live2dLipSyncAudioGraph = {
  analyser: Live2dLipSyncAnalyserNode;
  audioContext: Live2dLipSyncAudioContext;
  buffer: Float32Array;
  mediaStreamTrackCount: number | null;
  source: Live2dLipSyncAudioSourceNode;
  sourceKind: "media_element" | "media_stream";
};

export type Live2dLipSyncAudioSourceBinding = {
  connectAnalyserToDestination: boolean;
  mediaStreamTrackCount: number | null;
  source: Live2dLipSyncAudioSourceNode;
  sourceKind: "media_element" | "media_stream";
};

export type Live2dLipSyncControllerConfig = {
  createAudioContext?: () => Live2dLipSyncAudioContext;
  fftSize?: number;
  reportDiagnostic?(level: "warn" | "error", message: string, details?: unknown): void;
};

export type Live2dLipSyncUpdateSubscription =
  | {
      kind: "model_event";
      internalModel: Live2dLipSyncInternalModel;
    }
  | {
      kind: "ticker";
      ticker: Live2dLipSyncTicker;
    };

export type Live2dAudioFrameAnalysis = {
  decibels: number;
  level: number;
  rms: number;
};

export type Live2dMouthParameterDiagnostic = {
  parameterId: string | null;
  parameterValueAfter: number | null;
  parameterValueBefore: number | null;
};
