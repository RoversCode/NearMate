import { shouldAcceptTimedReminderSnapshot } from "../lib/timedReminderSnapshotAcceptance";
import type { TimedReminderSnapshot } from "../types/backendEvents";
import type { TimedReminderSoundPlayer } from "./avatarTimedReminderSoundPlayer";

// 收集一秒内到点的事项，避免同一批提醒接连发出多轮提示音。
const REMINDER_BATCH_WINDOW_MS = 1000;
const REMINDER_SHAKE_DURATION_MS = 500;

/**
 * 提供提醒反馈使用的本地音频与动画出口。
 *
 * Args:
 *   player: 只播放本地短音效的播放器。
 *   onShakeChanged: 开始或结束一次视觉抖动，不改变桌宠位置。
 */
type TimedReminderFeedbackOptions = {
  player: TimedReminderSoundPlayer;
  onShakeChanged(active: boolean): void;
};

/**
 * 根据权威快照合并响铃批次，并管理短音效和动画的生命周期。
 *
 * Args:
 *   options: 本地播放器和动画状态回调。
 *
 * Returns:
 *   接受快照、停止反馈和销毁资源的协调器；停止反馈不会关闭后端提醒。
 */
export function createTimedReminderFeedback(options: TimedReminderFeedbackOptions) {
  let snapshot: TimedReminderSnapshot | null = null;
  const highestAttemptById = new Map<string, number>();
  const pendingIds = new Set<string>();
  let playingIds = new Set<string>();
  let batchTimer: ReturnType<typeof setTimeout> | null = null;
  let shakeTimer: ReturnType<typeof setTimeout> | null = null;
  let destroyed = false;

  /** 当前批次关闭时只停当前声音，后到的无关事项仍需继续提醒。 */
  function stopPlayback(): void {
    if (shakeTimer !== null) clearTimeout(shakeTimer);
    shakeTimer = null;
    playingIds.clear();
    options.player.stop();
    options.onShakeChanged(false);
  }

  /** 停止全部反馈，保留去重记录，避免下一份相同快照再次打扰用户。 */
  function stop(): void {
    if (batchTimer !== null) clearTimeout(batchTimer);
    batchTimer = null;
    pendingIds.clear();
    stopPlayback();
  }

  /** 合并窗口结束且上一轮声音播完后，才允许开始下一轮反馈。 */
  function playPendingBatch(): void {
    if (destroyed || batchTimer !== null || playingIds.size > 0 || pendingIds.size === 0) {
      return;
    }
    playingIds = new Set(pendingIds);
    pendingIds.clear();
    options.onShakeChanged(true);
    shakeTimer = setTimeout(() => {
      shakeTimer = null;
      options.onShakeChanged(false);
    }, REMINDER_SHAKE_DURATION_MS);
    options.player.play(() => {
      playingIds.clear();
      // 播放失败可能早于动画结束；后续批次仍须等动画回到原位。
      if (pendingIds.size > 0 && batchTimer === null) scheduleBatch();
    });
  }

  /** 固定从首条新提醒开始计时，后来的快照不能无限推迟已经到点的反馈。 */
  function scheduleBatch(): void {
    if (batchTimer !== null) return;
    batchTimer = setTimeout(() => {
      batchTimer = null;
      playPendingBatch();
    }, REMINDER_BATCH_WINDOW_MS);
  }

  return {
    /**
     * 从新快照中领取尚未反馈的响铃次数，并停止已经关闭的批次。
     *
     * Args:
     *   next: Avatar 收到的完整提醒快照。
     */
    update(next: TimedReminderSnapshot): void {
      if (destroyed || !next.runtime_id || !shouldAcceptTimedReminderSnapshot(snapshot, next)) {
        return;
      }
      const runtimeChanged = snapshot?.runtime_id !== next.runtime_id;
      snapshot = next;
      if (runtimeChanged) {
        stop();
        highestAttemptById.clear();
      }

      const ringingIds = new Set(next.active
        .filter(reminder => reminder.state === "ringing")
        .map(reminder => reminder.reminder_id));
      // 关闭同一播报批次的任一事项时，停止整轮声音，与现有批次关闭体验一致。
      if ([...playingIds].some(id => !ringingIds.has(id))) stopPlayback();
      for (const id of pendingIds) {
        if (!ringingIds.has(id)) pendingIds.delete(id);
      }

      const activeIds = new Set(next.active.map(reminder => reminder.reminder_id));
      for (const id of highestAttemptById.keys()) {
        if (!activeIds.has(id)) highestAttemptById.delete(id);
      }
      for (const reminder of next.active) {
        if (reminder.state !== "ringing") continue;
        // 到点时次数为零，首次成功入队才变成一；两者只能响一轮。
        const attempt = Math.max(1, reminder.ring_count);
        const previousAttempt = highestAttemptById.get(reminder.reminder_id) ?? 0;
        highestAttemptById.set(reminder.reminder_id, Math.max(previousAttempt, attempt));
        // 首次水合只建立基线；重新打开窗口不能补播已经存在的提醒。
        if (!runtimeChanged && attempt > previousAttempt) pendingIds.add(reminder.reminder_id);
      }
      if (pendingIds.size > 0) scheduleBatch();
    },
    stop,
    /**
     * 用户请求关闭指定事项时，只停止与这些编号相交的本地反馈。
     *
     * Args:
     *   reminderIds: 仍在等待关闭回执的事项编号。
     */
    stopReminders(reminderIds: ReadonlySet<string>): void {
      for (const id of reminderIds) pendingIds.delete(id);
      if ([...playingIds].some(id => reminderIds.has(id))) stopPlayback();
    },
    /** 停止全部本地资源，销毁后不再消费快照。 */
    destroy(): void {
      destroyed = true;
      stop();
      highestAttemptById.clear();
    }
  };
}
