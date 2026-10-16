import {
  DEFAULT_ANALYSER_SMOOTHING,
  DEFAULT_MAX_DECIBELS,
  DEFAULT_MIN_DECIBELS
} from "./live2dAudioAnalysis";
import { toRecord } from "./live2dMouthWriter";
import type {
  Live2dLipSyncAudioContext,
  Live2dLipSyncAudioGraph,
  Live2dLipSyncAudioSourceBinding
} from "./live2dLipSyncTypes";

type CreateLive2dLipSyncAudioGraphInput = {
  audioContext: Live2dLipSyncAudioContext;
  element: HTMLMediaElement;
  fftSize: number;
};

/**
 * 创建 Live2D 唇形同步使用的 Web Audio 分析图。
 *
 * Args:
 *   input: AudioContext、播放元素与 FFT 配置。
 *
 * Returns:
 *   已连接 source、analyser 和分析缓冲区。
 */
export function createLive2dLipSyncAudioGraph(
  input: CreateLive2dLipSyncAudioGraphInput
): Live2dLipSyncAudioGraph {
  const sourceBinding = createLive2dLipSyncAudioSourceBinding(
    input.audioContext,
    input.element
  );
  const analyser = input.audioContext.createAnalyser();
  analyser.fftSize = input.fftSize;
  analyser.minDecibels = DEFAULT_MIN_DECIBELS;
  analyser.maxDecibels = DEFAULT_MAX_DECIBELS;
  analyser.smoothingTimeConstant = DEFAULT_ANALYSER_SMOOTHING;
  sourceBinding.source.connect(analyser);

  if (sourceBinding.connectAnalyserToDestination) {
    analyser.connect(input.audioContext.destination);
  }

  return {
    analyser,
    audioContext: input.audioContext,
    buffer: new Float32Array(analyser.fftSize),
    mediaStreamTrackCount: sourceBinding.mediaStreamTrackCount,
    source: sourceBinding.source,
    sourceKind: sourceBinding.sourceKind
  };
}

/**
 * 尝试恢复浏览器挂起的 AudioContext。
 *
 * Args:
 *   audioContext: 当前音频图使用的上下文。
 *   reportError: 恢复失败时的诊断回调。
 */
export function resumeLive2dLipSyncAudioContext(
  audioContext: Live2dLipSyncAudioContext,
  reportError: (error: unknown) => void
): void {
  if (audioContext.state !== "suspended") {
    return;
  }

  void audioContext.resume?.().catch(reportError);
}

/**
 * 断开 Web Audio 图并关闭上下文。
 *
 * Args:
 *   graph: 当前音频分析图。
 *   reportCloseError: AudioContext 关闭失败时的诊断回调。
 */
export function disconnectLive2dLipSyncAudioGraph(
  graph: Live2dLipSyncAudioGraph,
  reportCloseError: (error: unknown) => void
): void {
  try {
    graph.source.disconnect();
  } catch {
    // Web Audio 节点可能已被浏览器自动断开；释放路径保持幂等。
  }

  try {
    graph.analyser.disconnect();
  } catch {
    // Web Audio 节点可能已被浏览器自动断开；释放路径保持幂等。
  }

  void graph.audioContext.close?.().catch(reportCloseError);
}

/**
 * 创建浏览器原生 AudioContext。
 *
 * Returns:
 *   当前浏览器支持的 AudioContext 实例。
 */
export function createBrowserAudioContext(): Live2dLipSyncAudioContext {
  const AudioContextConstructor =
    window.AudioContext ??
    (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;

  if (!AudioContextConstructor) {
    throw new Error("当前浏览器不支持 Web Audio AudioContext。");
  }

  return new AudioContextConstructor();
}

function createLive2dLipSyncAudioSourceBinding(
  audioContext: Live2dLipSyncAudioContext,
  element: HTMLMediaElement
): Live2dLipSyncAudioSourceBinding {
  const mediaStream = readAudioElementMediaStream(element);
  if (mediaStream && typeof audioContext.createMediaStreamSource === "function") {
    const audioTracks = mediaStream.getAudioTracks();
    if (audioTracks.length > 0) {
      return {
        connectAnalyserToDestination: false,
        mediaStreamTrackCount: audioTracks.length,
        source: audioContext.createMediaStreamSource(mediaStream),
        sourceKind: "media_stream"
      };
    }
  }

  return {
    connectAnalyserToDestination: true,
    mediaStreamTrackCount: mediaStream?.getAudioTracks().length ?? null,
    source: audioContext.createMediaElementSource(element),
    sourceKind: "media_element"
  };
}

function readAudioElementMediaStream(element: HTMLMediaElement): MediaStream | null {
  const srcObject = (element as HTMLMediaElement & { srcObject?: unknown }).srcObject;
  const candidate = toRecord(srcObject);

  if (!candidate || typeof candidate.getAudioTracks !== "function") {
    return null;
  }

  /**
   * 业务意图：LiveKit attach 的 audio 元素通常通过 srcObject 承载远端 MediaStream。
   * 这里按结构识别，避免测试环境或 Electron 版本缺少全局 MediaStream 构造函数时误判。
   */
  return srcObject as MediaStream;
}
