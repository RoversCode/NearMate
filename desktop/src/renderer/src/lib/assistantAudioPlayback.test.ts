// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import { AssistantAudioPlayback } from "./assistantAudioPlayback";

/**
 * 构造可被 AssistantAudioPlayback attach 的测试音轨。
 *
 * 核心入参：audioElement 是 attach 后返回的受控音频元素。
 * 预期输出：带 attach/detach mock 的最小远端音轨替身。
 * 边界异常：测试替身不模拟真实媒体流，只验证播放边界是否正确管理 DOM 和 LiveKit attach 生命周期。
 */
function createAttachableTrack(audioElement: HTMLAudioElement) {
  return {
    attach: vi.fn(() => audioElement),
    detach: vi.fn(() => [audioElement])
  };
}

describe("AssistantAudioPlayback", () => {
  it("快捷键立即静音，并让迟到的新音轨保持静音直到明确恢复", async () => {
    const first = document.createElement("audio");
    first.play = vi.fn(async () => undefined);
    const playback = new AssistantAudioPlayback(document);
    await playback.playTrack(createAttachableTrack(first));
    playback.setMuted(true);
    expect(first.muted).toBe(true);
    const late = document.createElement("audio");
    late.play = vi.fn(async () => { expect(late.muted).toBe(true); });
    await playback.playTrack(createAttachableTrack(late));
    expect(late.muted).toBe(true);
    playback.setMuted(false);
    expect(late.muted).toBe(false);
  });

  it("已经释放的异步 play 完成不能重新发布旧播放元素", async () => {
    const element = document.createElement("audio");
    let finish!: () => void;
    element.play = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    const changed = vi.fn();
    const playback = new AssistantAudioPlayback(document, { onPlaybackElementChanged: changed });
    const pending = playback.playTrack(createAttachableTrack(element));
    playback.release();
    finish();
    await pending;
    expect(changed).not.toHaveBeenCalledWith(element);
  });

  it.each([false, true])("音频元素被新轨复用后，旧 play 的迟到结果不能影响新轨（失败：%s）", async fails => {
    const element = document.createElement("audio");
    let resolveOldPlay!: () => void;
    let rejectOldPlay!: (error: Error) => void;
    const oldPlay = new Promise<void>((resolve, reject) => {
      resolveOldPlay = resolve;
      rejectOldPlay = reject;
    });
    element.play = vi.fn(async (): Promise<void> => undefined).mockReturnValueOnce(oldPlay);
    const oldTrack = createAttachableTrack(element);
    const newTrack = createAttachableTrack(element);
    const changed = vi.fn();
    const playback = new AssistantAudioPlayback(document, { onPlaybackElementChanged: changed });
    const pending = playback.playTrack(oldTrack);
    const oldResult = fails ? expect(pending).rejects.toThrow("old play aborted") : pending;

    // LiveKit 会复用 detach 后的 audio 元素，DOM 对象相同并不代表仍是同次播放。
    await playback.playTrack(newTrack);
    changed.mockClear();
    if (fails) rejectOldPlay(new Error("old play aborted"));
    else resolveOldPlay();
    await oldResult;

    expect(newTrack.detach).not.toHaveBeenCalled();
    expect(element.muted).toBe(false);
    expect(document.body.contains(element)).toBe(true);
    expect(changed).not.toHaveBeenCalled();
    playback.release();
  });
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("把助手远端音轨 attach 到隐藏 audio 元素并调用播放", async () => {
    const audioElement = document.createElement("audio");
    audioElement.play = vi.fn(async () => undefined);
    const track = createAttachableTrack(audioElement);
    const playback = new AssistantAudioPlayback(document);

    await playback.playTrack(track);

    expect(track.attach).toHaveBeenCalledTimes(1);
    expect(audioElement.autoplay).toBe(true);
    expect(audioElement.getAttribute("data-companion-assistant-audio")).toBe("true");
    expect(audioElement.style.display).toBe("none");
    expect(document.body.contains(audioElement)).toBe(true);
    expect(audioElement.play).toHaveBeenCalledTimes(1);
  });

  it("播放成功后上报当前 audio 元素，释放时上报 null", async () => {
    const audioElement = document.createElement("audio");
    audioElement.play = vi.fn(async () => undefined);
    const track = createAttachableTrack(audioElement);
    const onPlaybackElementChanged = vi.fn();
    const playback = new AssistantAudioPlayback(document, {
      onPlaybackElementChanged
    });

    await playback.playTrack(track);
    playback.release();

    expect(onPlaybackElementChanged).toHaveBeenNthCalledWith(1, audioElement);
    expect(onPlaybackElementChanged).toHaveBeenNthCalledWith(2, null);
  });

  it("播放失败时上报 null 并释放已 attach 的元素", async () => {
    const audioElement = document.createElement("audio");
    audioElement.play = vi.fn(async () => {
      throw new Error("autoplay denied");
    });
    const track = createAttachableTrack(audioElement);
    const onPlaybackElementChanged = vi.fn();
    const playback = new AssistantAudioPlayback(document, {
      onPlaybackElementChanged
    });

    await expect(playback.playTrack(track)).rejects.toThrow("autoplay denied");

    expect(onPlaybackElementChanged).toHaveBeenCalledWith(null);
    expect(track.detach).toHaveBeenCalledOnce();
    expect(document.body.contains(audioElement)).toBe(false);
  });

  it("播放新音轨前释放旧音轨和旧 DOM 元素", async () => {
    const firstAudioElement = document.createElement("audio");
    firstAudioElement.play = vi.fn(async () => undefined);
    const firstTrack = createAttachableTrack(firstAudioElement);
    const secondAudioElement = document.createElement("audio");
    secondAudioElement.play = vi.fn(async () => undefined);
    const secondTrack = createAttachableTrack(secondAudioElement);
    const playback = new AssistantAudioPlayback(document);

    await playback.playTrack(firstTrack);
    await playback.playTrack(secondTrack);

    expect(firstTrack.detach).toHaveBeenCalledTimes(1);
    expect(document.body.contains(firstAudioElement)).toBe(false);
    expect(document.body.contains(secondAudioElement)).toBe(true);
  });

  it("release 会 detach 当前音轨并移除 audio 元素", async () => {
    const audioElement = document.createElement("audio");
    audioElement.play = vi.fn(async () => undefined);
    const track = createAttachableTrack(audioElement);
    const playback = new AssistantAudioPlayback(document);

    await playback.playTrack(track);
    playback.release();

    expect(track.detach).toHaveBeenCalledTimes(1);
    expect(document.body.contains(audioElement)).toBe(false);
  });

  it("缺少 attach 的音轨会抛出清晰错误", async () => {
    const playback = new AssistantAudioPlayback(document);

    await expect(playback.playTrack({})).rejects.toThrow("助手音频轨缺少 attach 方法");
  });
});
