import deleteSoundUrl from "../../../../../assets/audio/material_del.mp3?url";
import uploadSoundUrl from "../../../../../assets/audio/upload.mp3?url";

export type AvatarMaterialDeliveryFeedback =
  | {
      operation: "add";
      kind: "progress" | "success" | "warning";
      addedCount: number;
      submittedCount: number;
      text: string;
      sound?: "upload";
    }
  | {
      operation: "remove";
      kind: "success";
      text: string;
      sound: "delete";
    }
  | {
      operation: "preflight";
      kind: "warning";
      text: string;
    };

type MaterialSound = "upload" | "delete";
type AudioPort = Pick<HTMLAudioElement, "currentTime" | "pause" | "play">;

type MaterialDeliverySoundPlayerOptions = {
  createAudio?: (src: string) => AudioPort;
  reportPlaybackError(error: unknown): void;
};

export type MaterialDeliverySoundPlayer = {
  play(sound: MaterialSound): void;
};

/**
 * 读取主进程发送的材料反馈联合协议。
 *
 * @param payload preload 转发给 renderer 的未知事件。
 * @returns 通过操作类型和音效约束校验的反馈；非法载荷返回 null。
 */
export function readAvatarMaterialDeliveryFeedback(
  payload: unknown
): AvatarMaterialDeliveryFeedback | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const candidate = payload as Record<string, unknown>;
  if (typeof candidate.text !== "string" || !candidate.text.trim()) {
    return null;
  }

  if (candidate.operation === "remove") {
    return candidate.kind === "success" && candidate.sound === "delete"
      ? { operation: "remove", kind: "success", text: candidate.text.trim(), sound: "delete" }
      : null;
  }
  if (candidate.operation === "preflight") {
    return candidate.kind === "warning" && candidate.sound === undefined
      ? { operation: "preflight", kind: "warning", text: candidate.text.trim() }
      : null;
  }
  if (candidate.operation !== "add"
    || (candidate.kind !== "progress" && candidate.kind !== "success" && candidate.kind !== "warning")
    || !isNonNegativeInteger(candidate.addedCount)
    || !isNonNegativeInteger(candidate.submittedCount)
    || candidate.addedCount > candidate.submittedCount
    || (candidate.sound !== undefined && candidate.sound !== "upload")
    || (candidate.kind === "progress" && (candidate.addedCount !== 0 || candidate.sound !== undefined))
    || (candidate.kind !== "progress" && (candidate.addedCount > 0) !== (candidate.sound === "upload"))) {
    return null;
  }
  return {
    operation: "add",
    kind: candidate.kind,
    addedCount: candidate.addedCount,
    submittedCount: candidate.submittedCount,
    text: candidate.text.trim(),
    ...(candidate.sound === "upload" ? { sound: "upload" as const } : {})
  };
}

/**
 * 创建复用音频元素的材料音效播放器。
 *
 * @param options Audio 工厂和播放失败诊断回调。
 * @returns 连续同类操作会停止旧声音并从头播放的播放器。
 */
export function createMaterialDeliverySoundPlayer(
  options: MaterialDeliverySoundPlayerOptions
): MaterialDeliverySoundPlayer {
  const sources: Record<MaterialSound, string> = {
    upload: uploadSoundUrl,
    delete: deleteSoundUrl
  };
  const audioBySound = new Map<MaterialSound, AudioPort>();

  return {
    play(sound) {
      const audio = audioBySound.get(sound)
        ?? options.createAudio?.(sources[sound])
        ?? new Audio(sources[sound]);
      audioBySound.set(sound, audio);
      audio.pause();
      audio.currentTime = 0;
      void audio.play().catch(error => {
        options.reportPlaybackError(error);
      });
    }
  };
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
