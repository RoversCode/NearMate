import { isConsoleSettings, type ConsoleSettings } from "./settings";

/**
 * 描述说话人白名单录音样本。
 *
 * 核心字段：id 是后端槽位编号，audio_url 是同源试听地址，created_at 用于展示录制顺序。
 * 预期用途：设置页展示、试听、替换和删除白名单录音。
 * 边界异常：前端不直接消费本地文件路径，避免泄露后端文件系统结构。
 */
type SpeakerVerificationSample = {
  id: string;
  audio_url: string;
  created_at: number;
};

/**
 * 描述说话人确认控制台状态。
 *
 * 核心字段：enabled 是当前开关，samples 是 console 录音白名单，can_enable 表示是否满足开启前置条件。
 * 预期用途：设置页驱动开关禁用态、录音槽位和保存结果刷新。
 * 边界异常：max_sample_count 与 required_speech_seconds 来自后端事实源，避免前端写死策略后漂移。
 */
export type SpeakerVerificationProfile = {
  enabled: boolean;
  samples: SpeakerVerificationSample[];
  max_sample_count: number;
  required_speech_seconds: number;
  can_enable: boolean;
  /** 说话人配置 mutation 成功时返回的统一设置提交点快照；旧 Backend 可省略。 */
  settings?: ConsoleSettings;
};

/**
 * 描述后端录音 VAD 进度事件。
 *
 * 核心字段：type 区分进度、完成、拒绝和错误，speech_seconds 是累计有效人声秒数。
 * 预期用途：录音 WebSocket 收到事件后更新 UI 或自动停止录音。
 * 边界异常：error 事件可能只有 message，调用方需要容忍缺少秒数字段。
 */
export type SpeakerRecordingProgress = {
  type: "speech_progress" | "completed" | "rejected" | "error";
  speech_seconds?: number;
  required_speech_seconds?: number;
  recording_id?: string;
  message?: string;
};

export type SpeakerVerificationSamplePayload = {
  recording_id: string;
  replace_sample_id?: string | null;
};

/**
 * 判断未知响应是否符合说话人确认 profile 契约。
 *
 * Args:
 *   payload: 后端 JSON 响应。
 *
 * Returns:
 *   字段类型完整时返回 true。
 */
export function isSpeakerVerificationProfile(payload: unknown): payload is SpeakerVerificationProfile {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  return (
    typeof candidate.enabled === "boolean" &&
    Array.isArray(candidate.samples) &&
    candidate.samples.every(isSpeakerVerificationSample) &&
    typeof candidate.max_sample_count === "number" &&
    typeof candidate.required_speech_seconds === "number" &&
    typeof candidate.can_enable === "boolean" &&
    (candidate.settings === undefined || isConsoleSettings(candidate.settings))
  );
}

function isSpeakerVerificationSample(payload: unknown): payload is SpeakerVerificationSample {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.audio_url === "string" &&
    typeof candidate.created_at === "number"
  );
}
