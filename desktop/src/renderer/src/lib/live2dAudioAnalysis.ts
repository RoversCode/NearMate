import type { Live2dAudioFrameAnalysis } from "./live2dLipSyncTypes";

export const DEFAULT_FFT_SIZE = 512;
export const DEFAULT_MIN_DECIBELS = -90;
export const DEFAULT_MAX_DECIBELS = -10;
export const DEFAULT_ANALYSER_SMOOTHING = 0.85;
const DEFAULT_MOUTH_ATTACK_SMOOTHING = 0.55;
const DEFAULT_MOUTH_RELEASE_SMOOTHING = 0.35;
export const DEFAULT_SPEECH_SILENCE_FRAME_LIMIT = 8;

const SDK_AUDIO_FLOOR_DECIBELS = -100;

type CalculateLive2dMouthOpenValueOptions = {
  lipSyncGain: number;
  maxDecibels: number;
  minDecibels: number;
};

type AnalyzeLive2dAudioFrameOptions = {
  maxDecibels: number;
  minDecibels: number;
};

/**
 * calculateLive2dRms 计算当前音频采样窗口的均方根音量。
 *
 * Args:
 *   buffer: Web Audio analyser 输出的时域采样。
 *
 * Returns:
 *   当前窗口的 RMS 音量。
 */
function calculateLive2dRms(buffer: Float32Array): number {
  if (buffer.length === 0) {
    return 0;
  }

  let sumSquares = 0;
  for (const sample of buffer) {
    sumSquares += sample * sample;
  }

  return Math.sqrt(sumSquares / buffer.length);
}

/**
 * analyzeLive2dAudioFrame 复刻 SDK SoundManager.analyze 的音量归一化算法。
 *
 * Args:
 *   buffer: Web Audio analyser 输出的时域采样。
 *   options: analyser 的最小与最大分贝范围。
 *
 * Returns:
 *   RMS、分贝值与 SDK 同款一位小数归一化音量。
 */
export function analyzeLive2dAudioFrame(
  buffer: Float32Array,
  options: AnalyzeLive2dAudioFrameOptions
): Live2dAudioFrameAnalysis {
  const rms = calculateLive2dRms(buffer);
  const decibels = 20 * Math.log10(
    rms || Math.pow(10, SDK_AUDIO_FLOOR_DECIBELS / 20)
  );
  const decibelRange = options.maxDecibels - options.minDecibels;
  const scaledLevel = decibelRange === 0
    ? 0
    : clamp((decibels - options.minDecibels) / decibelRange, 0, 1);

  return {
    decibels,
    level: Number.parseFloat(scaledLevel.toFixed(1)),
    rms
  };
}

/**
 * calculateLive2dMouthOpenValue 将当前音频采样窗口转换为 Live2D 嘴部开合值。
 *
 * Args:
 *   buffer: Web Audio analyser 输出的时域采样。
 *   options: SDK lipSyncGain 与 analyser 分贝范围。
 *
 * Returns:
 *   归一化后的嘴部开合值，范围固定为 0 到 1。
 */
export function calculateLive2dMouthOpenValue(
  buffer: Float32Array,
  options: CalculateLive2dMouthOpenValueOptions
): number {
  if (buffer.length === 0) {
    return 0;
  }

  const analysis = analyzeLive2dAudioFrame(buffer, options);
  let value = analysis.level * options.lipSyncGain;
  value = Math.pow(value, 1.15);
  const minimumValue = value > 0 ? 0.1 : 0;

  return clamp(value, minimumValue, 1);
}

/**
 * calculateLive2dMouthTargetValue 根据说话状态决定嘴型目标值。
 *
 * Args:
 *   baseMouthValue: 模型当前表情或动作留下的嘴型基准。
 *   audioMouthValue: 音频分析得到的嘴型值。
 *   speechActive: 当前是否仍处于说话窗口。
 *
 * Returns:
 *   本帧应写入模型的嘴型目标值。
 */
export function calculateLive2dMouthTargetValue(
  baseMouthValue: number | null,
  audioMouthValue: number,
  speechActive: boolean
): number {
  const safeBaseMouthValue = typeof baseMouthValue === "number"
    ? clamp(baseMouthValue, 0, 1)
    : 0;
  const safeAudioMouthValue = clamp(audioMouthValue, 0, 1);

  if (speechActive) {
    return safeAudioMouthValue;
  }

  return safeBaseMouthValue;
}

/**
 * smoothLive2dMouthOpenValue 平滑嘴型变化，避免音量变化造成突兀跳变。
 *
 * Args:
 *   currentValue: 当前已经写入的嘴型值。
 *   targetValue: 本帧目标嘴型值。
 *   hasCurrentValue: 是否已经有上一帧可参考。
 *
 * Returns:
 *   平滑后的嘴型值。
 */
export function smoothLive2dMouthOpenValue(
  currentValue: number,
  targetValue: number,
  hasCurrentValue: boolean
): number {
  if (!hasCurrentValue) {
    return targetValue;
  }

  const smoothing = targetValue > currentValue
    ? DEFAULT_MOUTH_ATTACK_SMOOTHING
    : DEFAULT_MOUTH_RELEASE_SMOOTHING;

  return clamp(currentValue + (targetValue - currentValue) * smoothing, 0, 1);
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
