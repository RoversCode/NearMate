import {
  DEFAULT_FFT_SIZE,
  DEFAULT_SPEECH_SILENCE_FRAME_LIMIT,
  analyzeLive2dAudioFrame,
  calculateLive2dMouthOpenValue,
  calculateLive2dMouthTargetValue,
  smoothLive2dMouthOpenValue
} from "./live2dAudioAnalysis";
import {
  readLive2dLipSyncGain,
  readLive2dMouthParameterDiagnostic,
  writeLive2dMouthOpenValue
} from "./live2dMouthWriter";
import {
  createBrowserAudioContext,
  createLive2dLipSyncAudioGraph,
  disconnectLive2dLipSyncAudioGraph,
  resumeLive2dLipSyncAudioContext
} from "./live2dLipSyncAudioGraph";
import {
  createLive2dLipSyncFrameDiagnosticCounters,
  reportLive2dAudioBindingDiagnostic,
  reportLive2dFrameDiagnostic,
  reportLive2dModelBindingDiagnostic,
  type Live2dLipSyncFrameDiagnosticCounters
} from "./live2dLipSyncDiagnostics";
import {
  createLive2dLipSyncSpeechState,
  updateLive2dLipSyncSpeechState,
  type Live2dLipSyncSpeechState
} from "./live2dLipSyncSpeechState";
import {
  attachLive2dLipSyncUpdateSubscription,
  detachLive2dLipSyncUpdateSubscription
} from "./live2dLipSyncSubscription";
import type {
  Live2dAudioFrameAnalysis,
  Live2dLipSyncAudioContext,
  Live2dLipSyncAudioGraph,
  Live2dLipSyncControllerConfig,
  Live2dLipSyncModelBinding,
  Live2dLipSyncUpdateSubscription,
  Live2dMouthParameterDiagnostic
} from "./live2dLipSyncTypes";

export type { Live2dAudioFrameAnalysis, Live2dLipSyncAudioContext, Live2dLipSyncModel, Live2dLipSyncModelBinding, Live2dLipSyncTicker } from "./live2dLipSyncTypes";

/**
 * Live2dLipSyncController 负责把当前播放音频的音量投影到当前 Live2D 模型嘴型。
 */
export class Live2dLipSyncController {
  private readonly createAudioContext: () => Live2dLipSyncAudioContext;
  private readonly fftSize: number;
  private readonly reportDiagnostic?: Live2dLipSyncControllerConfig["reportDiagnostic"];
  private audioGraph: Live2dLipSyncAudioGraph | null = null;
  private diagnosticCounters: Live2dLipSyncFrameDiagnosticCounters =
    createLive2dLipSyncFrameDiagnosticCounters();
  private modelBinding: Live2dLipSyncModelBinding | null = null;
  private updateSubscription: Live2dLipSyncUpdateSubscription | null = null;
  private currentMouthValue = 0;
  private hasCurrentMouthValue = false;
  private speechState: Live2dLipSyncSpeechState = createLive2dLipSyncSpeechState();
  private readonly updateMouth = () => {
    try {
      this.updateMouthOnce();
    } catch (error) {
      this.reportDiagnostic?.("warn", "avatar.live2d_lip_sync_update_failed", error);
      this.detachUpdateSubscription();
    }
  };

  /**
   * 初始化唇形同步控制器。
   *
   * Args:
   *   config: Web Audio 工厂、音量映射参数和诊断回调。
   */
  constructor(config: Live2dLipSyncControllerConfig = {}) {
    this.createAudioContext =
      config.createAudioContext ?? createBrowserAudioContext;
    this.fftSize = config.fftSize ?? DEFAULT_FFT_SIZE;
    this.reportDiagnostic = config.reportDiagnostic;
  }

  /**
   * setAudioElement 绑定或解绑当前实际播放的助手音频元素。
   *
   * Args:
   *   element: 当前 LiveKit attach 得到的 audio 元素；传入 null 表示停止同步。
   */
  setAudioElement(element: HTMLMediaElement | null): void {
    this.disconnectAudioGraph();
    this.resetFrameDiagnostics();

    if (!element) {
      this.closeMouth();
      this.syncUpdateSubscription();
      return;
    }

    try {
      const audioContext = this.createAudioContext();
      this.audioGraph = createLive2dLipSyncAudioGraph({
        audioContext,
        element,
        fftSize: this.fftSize
      });
      resumeLive2dLipSyncAudioContext(audioContext, error => {
        this.reportDiagnostic?.("warn", "avatar.live2d_lip_sync_audio_resume_failed", error);
      });
      this.reportAudioBindingDiagnostic();
    } catch (error) {
      this.audioGraph = null;
      this.reportDiagnostic?.("warn", "avatar.live2d_lip_sync_audio_failed", error);
    }

    this.syncUpdateSubscription();
  }

  /**
   * setModelBinding 绑定或解绑当前真实显示中的 Live2D 模型。
   *
   * Args:
   *   binding: 当前模型与 Pixi ticker；传入 null 表示模型不可写。
   */
  setModelBinding(binding: Live2dLipSyncModelBinding | null): void {
    this.detachUpdateSubscription();
    this.modelBinding = binding;
    this.closeMouth();
    this.syncUpdateSubscription();
    this.reportModelBindingDiagnostic();
  }

  /**
   * destroy 释放 Web Audio、ticker 和模型引用。
   */
  destroy(): void {
    this.detachUpdateSubscription();
    this.closeMouth();
    this.modelBinding = null;
    this.disconnectAudioGraph();
  }

  private updateMouthOnce(): void {
    if (!this.audioGraph || !this.modelBinding) {
      return;
    }

    this.audioGraph.analyser.getFloatTimeDomainData(this.audioGraph.buffer);
    const analysis = analyzeLive2dAudioFrame(this.audioGraph.buffer, {
      maxDecibels: this.audioGraph.analyser.maxDecibels,
      minDecibels: this.audioGraph.analyser.minDecibels
    });
    const audioMouthValue = calculateLive2dMouthOpenValue(this.audioGraph.buffer, {
      lipSyncGain: readLive2dLipSyncGain(this.modelBinding.model),
      maxDecibels: this.audioGraph.analyser.maxDecibels,
      minDecibels: this.audioGraph.analyser.minDecibels
    });

    const parameterDiagnosticBefore = readLive2dMouthParameterDiagnostic(this.modelBinding.model);
    const speechUpdate = updateLive2dLipSyncSpeechState(
      this.speechState,
      audioMouthValue,
      DEFAULT_SPEECH_SILENCE_FRAME_LIMIT
    );
    this.speechState = speechUpdate.state;
    const mouthTargetValue = calculateLive2dMouthTargetValue(
      parameterDiagnosticBefore.parameterValue,
      audioMouthValue,
      speechUpdate.speechActive
    );
    this.currentMouthValue = smoothLive2dMouthOpenValue(
      this.currentMouthValue,
      mouthTargetValue,
      this.hasCurrentMouthValue
    );
    this.hasCurrentMouthValue = true;
    const writeResult = writeLive2dMouthOpenValue(this.modelBinding.model, this.currentMouthValue);
    const parameterDiagnosticAfter = readLive2dMouthParameterDiagnostic(this.modelBinding.model);
    this.reportFrameDiagnostic(analysis, this.currentMouthValue, {
      parameterId: parameterDiagnosticBefore.parameterId ?? parameterDiagnosticAfter.parameterId,
      parameterValueAfter: parameterDiagnosticAfter.parameterValue,
      parameterValueBefore: parameterDiagnosticBefore.parameterValue
    });

    if (!writeResult.written) {
      this.reportDiagnostic?.("warn", "avatar.live2d_lip_sync_model_unsupported", writeResult);
      this.detachUpdateSubscription();
    }
  }

  private syncUpdateSubscription(): void {
    this.detachUpdateSubscription();

    if (!this.audioGraph || !this.modelBinding) {
      return;
    }

    this.updateSubscription = attachLive2dLipSyncUpdateSubscription(
      this.modelBinding,
      this.updateMouth
    );
  }

  private reportModelBindingDiagnostic(): void {
    reportLive2dModelBindingDiagnostic(
      this.reportDiagnostic,
      this.modelBinding,
      this.updateSubscription
    );
  }

  private reportFrameDiagnostic(
    analysis: Live2dAudioFrameAnalysis,
    mouthValue: number,
    parameterDiagnostic: Live2dMouthParameterDiagnostic
  ): void {
    this.diagnosticCounters = reportLive2dFrameDiagnostic({
      reportDiagnostic: this.reportDiagnostic,
      counters: this.diagnosticCounters,
      analysis,
      mouthValue,
      parameterDiagnostic
    });
  }

  private reportAudioBindingDiagnostic(): void {
    reportLive2dAudioBindingDiagnostic(this.reportDiagnostic, this.audioGraph);
  }

  private resetFrameDiagnostics(): void {
    this.diagnosticCounters = createLive2dLipSyncFrameDiagnosticCounters();
    this.hasCurrentMouthValue = false;
    this.speechState = createLive2dLipSyncSpeechState();
  }

  private detachUpdateSubscription(): void {
    const subscription = this.updateSubscription;
    this.updateSubscription = null;
    detachLive2dLipSyncUpdateSubscription(subscription, this.updateMouth);
  }

  private closeMouth(): void {
    this.currentMouthValue = 0;
    this.hasCurrentMouthValue = false;
    this.speechState = createLive2dLipSyncSpeechState();
  }

  private disconnectAudioGraph(): void {
    const graph = this.audioGraph;
    this.audioGraph = null;

    if (!graph) {
      return;
    }

    disconnectLive2dLipSyncAudioGraph(graph, error => {
      this.reportDiagnostic?.("warn", "avatar.live2d_lip_sync_audio_close_failed", error);
    });
  }
}
