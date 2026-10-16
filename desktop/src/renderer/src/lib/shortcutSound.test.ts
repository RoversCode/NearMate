import { afterEach, describe, expect, it, vi } from "vitest";
import { ShortcutSound } from "./shortcutSound";

describe("快捷键专用短音效", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("产生低音量的 95ms 双音高提示，连按会停止前一次而不堆积", async () => {
    const nodes: Array<{ stop: ReturnType<typeof vi.fn> }> = [];
    const frequency = { setValueAtTime: vi.fn() };
    const gain = { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() };
    const context = {
      state: "running", currentTime: 12, destination: {}, close: vi.fn(async () => undefined),
      createOscillator: () => {
        const node = { frequency, connect: vi.fn(), start: vi.fn(), stop: vi.fn(), disconnect: vi.fn() };
        nodes.push(node);
        return node;
      },
      createGain: () => ({ gain, connect: vi.fn(), disconnect: vi.fn() })
    };
    vi.stubGlobal("AudioContext", class { constructor() { return context; } });
    const sound = new ShortcutSound();
    await sound.play();
    expect(frequency.setValueAtTime.mock.calls).toEqual([[740, 12], [988, 12.035]]);
    expect(gain.linearRampToValueAtTime).toHaveBeenCalledWith(0.07, 12.006);
    expect(nodes[0].stop).toHaveBeenCalledWith(12.095);
    await sound.play();
    expect(nodes[0].stop).toHaveBeenCalledTimes(2);
    sound.dispose();
    expect(nodes[1].stop).toHaveBeenCalledTimes(2);
    expect(context.close).toHaveBeenCalledOnce();
  });

  it("音频上下文尚在唤醒时卸载，不会产生迟到提示音", async () => {
    let resume!: () => void;
    const context = {
      state: "suspended", resume: () => new Promise<void>(resolve => { resume = resolve; }),
      close: vi.fn(async () => undefined), createOscillator: vi.fn()
    };
    vi.stubGlobal("AudioContext", class { constructor() { return context; } });
    const sound = new ShortcutSound();
    const pending = sound.play();
    sound.dispose();
    resume();
    await pending;
    expect(context.createOscillator).not.toHaveBeenCalled();
  });
});
