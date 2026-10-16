const ASSISTANT_AUDIO_TRACK_NAME = "assistant-audio";
const CLEAN_MICROPHONE_TRACK_NAME = "clean-mic";

/**
 * 判断订阅到的远端轨道是否是 backend 助手语音轨。
 *
 * 核心入参：track 和 publication 来自 LiveKit TrackSubscribed 事件。
 * 预期输出：只有音频类型且轨道名为 assistant-audio 时返回 true。
 * 边界异常：测试替身或 SDK 版本缺少字段时返回 false，只保留订阅诊断，不尝试播放未知轨道。
 */
export function isAssistantAudioTrack(track: unknown, publication: unknown): boolean {
  return isNamedAudioTrack(track, publication, ASSISTANT_AUDIO_TRACK_NAME);
}

/**
 * 判断订阅到的远端轨道是否是 AEC sidecar 发布的干净本地麦克风。
 *
 * Args:
 *   track: LiveKit TrackSubscribed 事件中的远端轨道。
 *   publication: LiveKit 轨道发布信息。
 *
 * Returns:
 *   只有音频类型且轨道名为 clean-mic 时返回 true。
 */
export function isCleanMicrophoneTrack(track: unknown, publication: unknown): boolean {
  return isNamedAudioTrack(track, publication, CLEAN_MICROPHONE_TRACK_NAME);
}

/**
 * isNamedAudioTrack 统一按媒体类型和轨道名识别产品音频轨。
 *
 * Args:
 *   track: LiveKit 远端轨道。
 *   publication: LiveKit 发布信息。
 *   expectedTrackName: 产品约定的精确轨道名。
 *
 * Returns:
 *   媒体类型为音频且轨道名精确匹配时返回 true。
 */
function isNamedAudioTrack(
  track: unknown,
  publication: unknown,
  expectedTrackName: string
): boolean {
  const trackRecord = toRecord(track);
  const publicationRecord = toRecord(publication);
  const kind = typeof trackRecord?.kind === "string" ? trackRecord.kind : "";
  const trackName =
    readStringField(publicationRecord, "trackName") ??
    readStringField(publicationRecord, "name") ??
    readStringField(trackRecord, "name");

  return kind.toLowerCase() === "audio" && trackName === expectedTrackName;
}

/**
 * 把未知对象窄化成可安全读取字段的记录。
 *
 * 核心入参：value 是 LiveKit SDK 或测试替身传入的未知对象。
 * 预期输出：对象记录或 null。
 * 边界异常：不读取原型链方法，避免把非对象值误当成轨道事实。
 */
function toRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  return value as Record<string, unknown>;
}

/**
 * 从记录中读取字符串字段。
 *
 * 核心入参：record 是可空记录，fieldName 是目标字段名。
 * 预期输出：字段为字符串时返回原值，否则返回 null。
 * 边界异常：空记录安全返回 null。
 */
function readStringField(record: Record<string, unknown> | null, fieldName: string): string | null {
  const value = record?.[fieldName];
  return typeof value === "string" ? value : null;
}
