import { describe, expect, it } from "vitest";
import {
  calculateCaptionUnitCount,
  createAssistantCaptionWindow,
  finishAssistantCaptionWindow,
  tickAssistantCaptionWindow
} from "./assistantCaptionWindow";

const calibratedTiming = {
  ms_per_unit: 100,
  calibration_text: "我在这里，听见你说话了。",
  duration_ms: 4000,
  unit_count: 40,
  measured_at: 1710000000
};

describe("assistantCaptionWindow", () => {
  it("中文长文本会按当前朗读进度裁剪成尾部字幕窗口", () => {
    const longText =
      "第一句先出现然后应该被挤出窗口。第二句继续铺垫气泡不要变长。第三句靠近当前语音位置。第四句才是最终可见的字幕内容。";
    const initialWindow = createAssistantCaptionWindow({
      text: longText,
      generationId: "generation-1",
      captionTiming: calibratedTiming
    });
    const tickedWindow = tickAssistantCaptionWindow(initialWindow, { deltaMs: 9200 });

    expect(tickedWindow.visibleText).not.toBe(longText);
    expect(tickedWindow.visibleText).not.toContain("第一句先出现");
    expect(tickedWindow.visibleText).toContain("第三句靠近当前语音位置");
    expect(calculateCaptionUnitCount(tickedWindow.visibleText)).toBeLessThanOrEqual(36);
  });

  it("字幕窗口容量贴近三行气泡，避免新文本先进入隐藏区域", () => {
    const longText =
      "哥哥，我明白你是想让我把事情说清楚，不过后面这句才是语音现在正在念的位置，所以它必须已经进入可见字幕。";
    const initialWindow = createAssistantCaptionWindow({
      text: longText,
      generationId: "generation-1",
      captionTiming: calibratedTiming
    });
    const tickedWindow = tickAssistantCaptionWindow(initialWindow, { deltaMs: 6200 });

    expect(tickedWindow.visibleText).not.toContain("哥哥，我明白");
    expect(tickedWindow.visibleText).toContain("后面这句才是语音现在正在念的位置");
    expect(calculateCaptionUnitCount(tickedWindow.visibleText)).toBeLessThanOrEqual(36);
  });

  it("英文、数字和标点折算后不会产生空窗口或单次大跳", () => {
    const initialWindow = createAssistantCaptionWindow({
      text: "CPU 100 percent is okay, wait... 我马上处理。",
      generationId: "generation-1",
      captionTiming: calibratedTiming
    });
    const tickedWindow = tickAssistantCaptionWindow(initialWindow, { deltaMs: 500 });

    expect(calculateCaptionUnitCount("CPU 100 percent,")).toBeGreaterThan(4);
    expect(tickedWindow.visibleText.trim().length).toBeGreaterThan(0);
    expect(tickedWindow.visibleText).toContain("CPU");
    expect(tickedWindow.visibleText).not.toContain("我马上处理");
  });

  it("文本未完全生成时字幕进度不会越过已收到文本", () => {
    const partialWindow = createAssistantCaptionWindow({
      text: "先说到这里，",
      generationId: "generation-1",
      captionTiming: calibratedTiming
    });
    const tickedWindow = tickAssistantCaptionWindow(partialWindow, { deltaMs: 20000 });

    expect(tickedWindow.visibleText).toBe("先说到这里，");
    expect(tickedWindow.progressUnitCount).toBe(calculateCaptionUnitCount("先说到这里，"));
  });

  it("最终文本播放到末尾后稳定显示尾句直到音频结束", () => {
    const finalWindow = createAssistantCaptionWindow({
      text: "前面的解释可以慢慢过去。最后这句话要留在气泡里。",
      generationId: "generation-1",
      captionTiming: calibratedTiming,
      textFinal: true
    });
    const tickedWindow = tickAssistantCaptionWindow(finalWindow, { deltaMs: 12000 });
    const finishedWindow = finishAssistantCaptionWindow(tickedWindow);

    expect(tickedWindow.visibleText).toContain("最后这句话要留在气泡里。");
    expect(finishedWindow.visibleText).toBe(tickedWindow.visibleText);
  });
});
