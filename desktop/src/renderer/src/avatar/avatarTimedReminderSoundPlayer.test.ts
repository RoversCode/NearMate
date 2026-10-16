// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createTimedReminderSoundPlayer } from "./avatarTimedReminderSoundPlayer";

/** 用真实媒体元素的事件模型验证串行顺序，只替换实际发声的方法。 */
function setup() {
  const audios: HTMLAudioElement[] = [];
  const createAudio = vi.fn((source: string) => {
    const audio = new Audio(source);
    audio.play = vi.fn(async () => undefined);
    audio.pause = vi.fn();
    audios.push(audio);
    return audio;
  });
  const reportPlaybackError = vi.fn();
  const done = vi.fn();
  const player = createTimedReminderSoundPlayer({ createAudio, reportPlaybackError });
  return { audios, createAudio, reportPlaybackError, done, player };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("提醒短音效播放器", () => {
  it("闹钟音结束才播放震动音，两段都不循环，正常结束释放监听器", () => {
    const { player, audios, createAudio, done } = setup();
    player.play(done);
    expect(createAudio).toHaveBeenCalledOnce();
    expect(decodeURI(audios[0].src)).toContain("闹钟.mp3");
    expect(audios[0].play).toHaveBeenCalledOnce();
    expect(audios[0].loop).toBe(false);
    audios[0].dispatchEvent(new Event("ended"));
    expect(createAudio).toHaveBeenCalledTimes(2);
    expect(decodeURI(audios[1].src)).toContain("震动.mp3");
    expect(audios[1].play).toHaveBeenCalledOnce();
    expect(audios[1].loop).toBe(false);
    expect(done).not.toHaveBeenCalled();
    audios[1].dispatchEvent(new Event("ended"));
    expect(done).toHaveBeenCalledOnce();
    expect(audios.every(audio => audio.onended === null && audio.onerror === null)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("停止第一段后迟到的结束事件不能播放第二段", () => {
    const { player, audios, createAudio, done } = setup();
    player.play(done);
    const lateEnded = audios[0].onended;
    player.stop();
    lateEnded?.call(audios[0], new Event("ended"));
    expect(audios[0].pause).toHaveBeenCalledOnce();
    expect(audios[0].onended).toBeNull();
    expect(createAudio).toHaveBeenCalledOnce();
    expect(done).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("停止第二段后不报告完成，也没有待执行计时器", () => {
    const { player, audios, done } = setup();
    player.play(done);
    audios[0].dispatchEvent(new Event("ended"));
    player.stop();
    expect(audios[1].pause).toHaveBeenCalledOnce();
    expect(done).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("替代播放后旧错误和旧结束回调都不能影响新一轮", () => {
    const { player, audios, done, reportPlaybackError } = setup();
    player.play(done);
    const lateEnded = audios[0].onended;
    const lateError = audios[0].onerror;
    player.play(done);
    lateEnded?.call(audios[0], new Event("ended"));
    lateError?.call(audios[0], new Event("error"));
    expect(audios).toHaveLength(2);
    expect(audios[1].pause).not.toHaveBeenCalled();
    expect(reportPlaybackError).not.toHaveBeenCalled();
    audios[1].dispatchEvent(new Event("ended"));
    expect(audios).toHaveLength(3);
    player.stop();
  });

  it.each(["reject", "throw", "event", "timeout", "factory"])("%s 失败只记录一次诊断并收口，不继续播放", async mode => {
    const { player, createAudio, audios, reportPlaybackError, done } = setup();
    const makeAudio = createAudio.getMockImplementation()!;
    createAudio.mockImplementation(source => {
      if (mode === "factory") throw new Error("无法创建音频");
      const audio = makeAudio(source);
      if (mode === "reject") audio.play = vi.fn(() => Promise.reject(new Error("播放被拒绝")));
      if (mode === "throw") audio.play = vi.fn(() => { throw new Error("播放异常"); });
      return audio;
    });
    player.play(done);
    if (mode === "event") audios[0].dispatchEvent(new Event("error"));
    if (mode === "timeout") vi.advanceTimersByTime(5000);
    await Promise.resolve();
    expect(reportPlaybackError).toHaveBeenCalledOnce();
    expect(done).toHaveBeenCalledOnce();
    expect(createAudio).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("停止后迟到的播放拒绝不再记录错误", async () => {
    const { player, createAudio, reportPlaybackError, done } = setup();
    const makeAudio = createAudio.getMockImplementation()!;
    let rejectPlay: (error: Error) => void = () => undefined;
    createAudio.mockImplementation(source => {
      const audio = makeAudio(source);
      audio.play = () => new Promise((_resolve, reject) => { rejectPlay = reject; });
      return audio;
    });
    player.play(done);
    player.stop();
    rejectPlay(new Error("迟到拒绝"));
    await Promise.resolve();
    expect(reportPlaybackError).not.toHaveBeenCalled();
    expect(done).not.toHaveBeenCalled();
  });
});
