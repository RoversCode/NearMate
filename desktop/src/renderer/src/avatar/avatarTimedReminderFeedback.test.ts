import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TimedReminderCompact, TimedReminderSnapshot } from "../types/backendEvents";
import { createTimedReminderFeedback } from "./avatarTimedReminderFeedback";

/** 创建完整响铃事项，测试只覆盖实际会变化的编号与次数。 */
function reminder(id = "reminder-a", ringCount = 1): TimedReminderCompact {
  return {
    reminder_id: id, content: "会议时间到了", state: "ringing",
    trigger_at: "2026-09-05T10:00:00+08:00", created_at: "2026-09-05T09:00:00+08:00",
    updated_at: "2026-09-05T10:00:00+08:00", finished_at: null,
    ring_count: ringCount, next_ring_at: null, finish_reason: null,
    can_modify: false, can_cancel: false, can_dismiss: true
  };
}

/** 创建指定运行期、版本和活跃事项的权威快照。 */
function snapshot(revision: number, active: TimedReminderCompact[] = [], runtimeId = "runtime-a"):
TimedReminderSnapshot {
  return { runtime_id: runtimeId, revision, active, recent: [] };
}

/** 用可控的播完回调验证串行批次，不依赖浏览器音频时间。 */
function setup() {
  const player = { play: vi.fn<(done: () => void) => void>(), stop: vi.fn() };
  const onShakeChanged = vi.fn();
  const feedback = createTimedReminderFeedback({ player, onShakeChanged });
  feedback.update(snapshot(0));
  return { player, onShakeChanged, feedback };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("提醒短反馈批次", () => {
  it("到点零次与首次入队一次合并，之后第二和第三次各响一轮", () => {
    const { feedback, player, onShakeChanged } = setup();
    feedback.update(snapshot(1, [reminder("a", 0)]));
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(1);
    expect(onShakeChanged).toHaveBeenLastCalledWith(true);
    vi.advanceTimersByTime(500);
    expect(onShakeChanged).toHaveBeenLastCalledWith(false);
    player.play.mock.calls[0][0]();
    feedback.update(snapshot(2, [reminder("a", 1)]));
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(1);
    for (const count of [2, 3]) {
      feedback.update(snapshot(count + 1, [reminder("a", count)]));
      vi.advanceTimersByTime(1000);
      expect(player.play).toHaveBeenCalledTimes(count);
      vi.advanceTimersByTime(500);
      player.play.mock.calls[count - 1][0]();
    }
    feedback.destroy();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("同一秒的多份快照合并一次，不把旧事项重新加入下一批", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(1, [reminder("a")]));
    vi.advanceTimersByTime(600);
    feedback.update(snapshot(2, [reminder("a"), reminder("b")]));
    vi.advanceTimersByTime(400);
    expect(player.play).toHaveBeenCalledTimes(1);
    feedback.update(snapshot(3, [reminder("a"), reminder("b")]));
    vi.advanceTimersByTime(500);
    player.play.mock.calls[0][0]();
    vi.advanceTimersByTime(5000);
    expect(player.play).toHaveBeenCalledTimes(1);
  });

  it("重复快照、旧版本、次数回退和重连时相同快照都不重播", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(3, [reminder("a", 2)]));
    vi.advanceTimersByTime(1500);
    player.play.mock.calls[0][0]();
    feedback.update(snapshot(3, [reminder("a", 2)]));
    feedback.update(snapshot(2, [reminder("a", 1)]));
    feedback.update(snapshot(4, [reminder("a", 1)]));
    feedback.update(snapshot(5, [reminder("a", 2)]));
    vi.advanceTimersByTime(3000);
    expect(player.play).toHaveBeenCalledTimes(1);
  });

  it("首次水合的旧响铃只建立基线，新运行期清除旧批次和计数", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(8, [reminder("a", 2)], "runtime-b"));
    vi.advanceTimersByTime(2000);
    expect(player.play).not.toHaveBeenCalled();
    feedback.update(snapshot(9, [reminder("a", 3)], "runtime-b"));
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(1);
    feedback.update(snapshot(0, [], "runtime-c"));
    expect(player.stop).toHaveBeenCalled();
    feedback.update(snapshot(1, [reminder("a", 0)], "runtime-c"));
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(2);
    feedback.destroy();
  });

  it("在合并窗口内关闭事项时不播放，另一条仍响铃的事项保留", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(1, [reminder("a"), reminder("b")]));
    feedback.update(snapshot(2, [reminder("b")]));
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(1);
    feedback.update(snapshot(3));
    expect(player.stop).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("关闭或第三次终态停止当前反馈，但其他无关快照不停止", () => {
    const { feedback, player, onShakeChanged } = setup();
    feedback.update(snapshot(1, [reminder("a", 3)]));
    vi.advanceTimersByTime(1000);
    player.stop.mockClear();
    feedback.update(snapshot(2, [reminder("a", 3)]));
    expect(player.stop).not.toHaveBeenCalled();
    feedback.update(snapshot(3));
    expect(player.stop).toHaveBeenCalledOnce();
    expect(onShakeChanged).toHaveBeenLastCalledWith(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("主动停止保留去重事实，关闭失败后的快照不会补播", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(1, [reminder()]));
    vi.advanceTimersByTime(1000);
    feedback.stop();
    feedback.update(snapshot(2, [reminder()]));
    vi.advanceTimersByTime(5000);
    expect(player.play).toHaveBeenCalledOnce();
  });

  it("上一轮未播完时下一批等待，播完后只播放一轮", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(1, [reminder("a")]));
    vi.advanceTimersByTime(1000);
    feedback.update(snapshot(2, [reminder("a"), reminder("b")]));
    vi.advanceTimersByTime(1500);
    expect(player.play).toHaveBeenCalledOnce();
    player.play.mock.calls[0][0]();
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(2);
    feedback.destroy();
  });

  it("销毁时取消待播和动画，并拒绝销毁后的快照", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(1, [reminder()]));
    feedback.destroy();
    feedback.update(snapshot(2, [reminder("b")]));
    vi.advanceTimersByTime(6000);
    expect(player.play).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("当前批次结束不能吞掉等待中的另一条提醒", () => {
    const { feedback, player } = setup();
    feedback.update(snapshot(1, [reminder("a", 3)]));
    vi.advanceTimersByTime(1000);
    feedback.update(snapshot(2, [reminder("a", 3), reminder("b")]));
    feedback.update(snapshot(3, [reminder("b")]));
    vi.advanceTimersByTime(1000);
    expect(player.play).toHaveBeenCalledTimes(2);
    feedback.destroy();
  });
});
