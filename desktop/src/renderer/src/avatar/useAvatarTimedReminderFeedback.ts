import { useCallback, useEffect, useRef, useState } from "react";
import type { TimedReminderSnapshot } from "../types/backendEvents";
import { createTimedReminderFeedback } from "./avatarTimedReminderFeedback";
import { createTimedReminderSoundPlayer } from "./avatarTimedReminderSoundPlayer";
import type { CompanionAvatarBridge } from "./avatarBridge";

/**
 * 把 Avatar 快照和关闭操作接到本地短反馈，卸载时释放全部音频与计时器。
 *
 * Args:
 *   snapshot: 当前权威提醒快照。
 *   requestingIds: 用户正在请求关闭的提醒编号。
 *   bridge: 仅用于上报短音效播放失败。
 *
 * Returns:
 *   视觉抖动状态与立即停止反馈的操作。
 */
export function useAvatarTimedReminderFeedback(
  snapshot: TimedReminderSnapshot | undefined,
  requestingIds: ReadonlySet<string> | undefined,
  bridge?: Pick<CompanionAvatarBridge, "reportDiagnostic">
) {
  const [feedbackActive, setFeedbackActive] = useState(false);
  const coordinatorRef = useRef<ReturnType<typeof createTimedReminderFeedback> | null>(null);
  const bridgeRef = useRef(bridge);
  bridgeRef.current = bridge;

  useEffect(() => {
    let mounted = true;
    const coordinator = createTimedReminderFeedback({
      player: createTimedReminderSoundPlayer({
        reportPlaybackError(error) {
          bridgeRef.current?.reportDiagnostic("warn", "avatar.timed_reminder_sound_failed", error);
        }
      }),
      onShakeChanged(active) {
        if (mounted) setFeedbackActive(active);
      }
    });
    coordinatorRef.current = coordinator;
    return () => {
      mounted = false;
      coordinator.destroy();
      coordinatorRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (snapshot) coordinatorRef.current?.update(snapshot);
    // 先吸收快照的去重事实，再静音；即使关闭请求失败，也不能自动重播旧声音。
    if (requestingIds?.size) coordinatorRef.current?.stopReminders(requestingIds);
  }, [snapshot, requestingIds]);

  const stopFeedback = useCallback(() => coordinatorRef.current?.stop(), []);
  return { feedbackActive, stopFeedback };
}
