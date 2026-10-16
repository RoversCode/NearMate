import type { CharacterCard } from "../../api/contracts/characterCards";
import type { EmotionGradingValue } from "./characterCardDraft";

/**
 * 格式化情绪分档词典为多行文本。
 *
 * Args:
 *   value: 角色卡里的情绪分档字典。
 *
 * Returns:
 *   可直接放入 textarea 的多行文本。
 */
export function formatEmotionGrading(value: EmotionGradingValue): string {
  return Object.entries(value)
    .map(([grade, phrase]) => `${grade}: ${phrase}`)
    .join("\n");
}

/**
 * 格式化字幕语速测算结果。
 *
 * Args:
 *   timing: 角色语音绑定里的字幕时序信息。
 *
 * Returns:
 *   面向控制台展示的简短说明。
 */
export function formatVoiceCaptionTiming(timing: CharacterCard["meta"]["voice_binding"]["caption_timing"]): string {
  if (timing === null) {
    return "字幕语速尚未测算";
  }

  return `每单位 ${Math.round(timing.ms_per_unit)}ms · 音频 ${formatDurationSeconds(timing.duration_ms)}s`;
}

/**
 * 压缩声纹文本为预览摘要。
 *
 * Args:
 *   voiceText: 角色声纹描述文本。
 *
 * Returns:
 *   最多 80 字符的单行摘要。
 */
export function summarizeVoice(voiceText: string): string {
  const normalizedVoice = voiceText.trim().replace(/\s+/g, " ");
  if (!normalizedVoice) {
    return "未设置";
  }
  return normalizedVoice.length > 80 ? `${normalizedVoice.slice(0, 80)}...` : normalizedVoice;
}

function formatDurationSeconds(durationMs: number): string {
  const durationSeconds = durationMs / 1000;
  return Number.isInteger(durationSeconds) ? String(durationSeconds) : durationSeconds.toFixed(1);
}
