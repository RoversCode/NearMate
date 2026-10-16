import {
  readLive2dLipSyncParameterIds,
  resolveLive2dMouthParameterWriterKind
} from "./live2dMouthWriter";
import type {
  Live2dAudioFrameAnalysis,
  Live2dLipSyncAudioGraph,
  Live2dLipSyncControllerConfig,
  Live2dLipSyncModelBinding,
  Live2dLipSyncUpdateSubscription,
  Live2dMouthParameterDiagnostic
} from "./live2dLipSyncTypes";

const SILENT_FRAME_DIAGNOSTIC_LIMIT = 2;
const VOICED_FRAME_DIAGNOSTIC_LIMIT = 5;

export type Live2dLipSyncFrameDiagnosticCounters = {
  silentDiagnosticFrameCount: number;
  voicedDiagnosticFrameCount: number;
};

/**
 * 创建帧级诊断计数器。
 *
 * Returns:
 *   静音帧和有声帧各自独立的限流计数。
 */
export function createLive2dLipSyncFrameDiagnosticCounters(): Live2dLipSyncFrameDiagnosticCounters {
  return {
    silentDiagnosticFrameCount: 0,
    voicedDiagnosticFrameCount: 0
  };
}

/**
 * 报告模型绑定诊断。
 *
 * Args:
 *   reportDiagnostic: renderer 诊断出口。
 *   modelBinding: 当前模型绑定。
 *   updateSubscription: 当前逐帧更新订阅。
 */
export function reportLive2dModelBindingDiagnostic(
  reportDiagnostic: Live2dLipSyncControllerConfig["reportDiagnostic"],
  modelBinding: Live2dLipSyncModelBinding | null,
  updateSubscription: Live2dLipSyncUpdateSubscription | null
): void {
  if (!modelBinding) {
    return;
  }

  reportDiagnostic?.("warn", "avatar.live2d_lip_sync_model_bound", {
    lipSyncIds: readLive2dLipSyncParameterIds(modelBinding.model),
    subscriptionKind: updateSubscription?.kind ?? "none",
    writerKind: resolveLive2dMouthParameterWriterKind(modelBinding.model)
  });
}

/**
 * 报告音频图绑定诊断。
 *
 * Args:
 *   reportDiagnostic: renderer 诊断出口。
 *   graph: 当前音频分析图。
 */
export function reportLive2dAudioBindingDiagnostic(
  reportDiagnostic: Live2dLipSyncControllerConfig["reportDiagnostic"],
  graph: Live2dLipSyncAudioGraph | null
): void {
  if (!graph) {
    return;
  }

  reportDiagnostic?.("warn", "avatar.live2d_lip_sync_audio_bound", {
    audioSourceKind: graph.sourceKind,
    mediaStreamTrackCount: graph.mediaStreamTrackCount
  });
}

/**
 * 按静音/有声帧分别限流并报告嘴型帧诊断。
 *
 * Args:
 *   input: 当前帧分析结果、嘴型值、参数读写快照和诊断计数。
 *
 * Returns:
 *   更新后的诊断计数器。
 */
export function reportLive2dFrameDiagnostic(input: {
  reportDiagnostic: Live2dLipSyncControllerConfig["reportDiagnostic"];
  counters: Live2dLipSyncFrameDiagnosticCounters;
  analysis: Live2dAudioFrameAnalysis;
  mouthValue: number;
  parameterDiagnostic: Live2dMouthParameterDiagnostic;
}): Live2dLipSyncFrameDiagnosticCounters {
  const hasVoice = input.analysis.rms > 0 || input.mouthValue > 0;

  if (hasVoice) {
    if (input.counters.voicedDiagnosticFrameCount >= VOICED_FRAME_DIAGNOSTIC_LIMIT) {
      return input.counters;
    }

    reportFrame(input);
    return {
      ...input.counters,
      voicedDiagnosticFrameCount: input.counters.voicedDiagnosticFrameCount + 1
    };
  }

  if (input.counters.silentDiagnosticFrameCount >= SILENT_FRAME_DIAGNOSTIC_LIMIT) {
    return input.counters;
  }

  reportFrame(input);
  return {
    ...input.counters,
    silentDiagnosticFrameCount: input.counters.silentDiagnosticFrameCount + 1
  };
}

function reportFrame(input: {
  reportDiagnostic: Live2dLipSyncControllerConfig["reportDiagnostic"];
  analysis: Live2dAudioFrameAnalysis;
  mouthValue: number;
  parameterDiagnostic: Live2dMouthParameterDiagnostic;
}): void {
  input.reportDiagnostic?.("warn", "avatar.live2d_lip_sync_frame", {
    decibels: input.analysis.decibels,
    level: input.analysis.level,
    mouthValue: input.mouthValue,
    parameterId: input.parameterDiagnostic.parameterId,
    parameterValueAfter: input.parameterDiagnostic.parameterValueAfter,
    parameterValueBefore: input.parameterDiagnostic.parameterValueBefore,
    rms: input.analysis.rms
  });
}
