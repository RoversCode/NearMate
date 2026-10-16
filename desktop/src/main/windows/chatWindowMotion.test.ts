import { describe, expect, it } from "vitest";
import {
  CHAT_WINDOW_COLLAPSED_SIZE,
  CHAT_WINDOW_EXPANDED_SIZE,
  createChatWindowMotion,
  resolveChatWindowBounds
} from "./chatWindowMotion";

describe("chatWindowMotion", () => {
  it("折叠态聊天球锚定到屏幕左下安全区域", () => {
    const bounds = resolveChatWindowBounds({
      mode: "collapsed",
      workArea: { x: 100, y: 40, width: 1280, height: 720 }
    });

    expect(bounds).toEqual({
      x: 124,
      y: 40 + 720 - CHAT_WINDOW_COLLAPSED_SIZE.height - 36,
      width: CHAT_WINDOW_COLLAPSED_SIZE.width,
      height: CHAT_WINDOW_COLLAPSED_SIZE.height
    });
  });

  it("小屏展开态会缩到工作区安全边距内", () => {
    const bounds = resolveChatWindowBounds({
      mode: "expanded",
      workArea: { x: 0, y: 0, width: 420, height: 520 }
    });

    expect(bounds).toEqual({ x: 16, y: 16, width: 388, height: 488 });
  });

  it("分别记忆折叠锚点和用户调整后的展开尺寸", () => {
    const motion = createChatWindowMotion();
    const workArea = { x: 0, y: 0, width: 1280, height: 900 };

    motion.resolveBounds("collapsed", workArea);
    motion.rememberBounds({ x: 100, y: 700, width: 96, height: 96 });
    expect(motion.resolveBounds("expanded", workArea)).toEqual({
      x: 100,
      y: 156,
      width: CHAT_WINDOW_EXPANDED_SIZE.width,
      height: CHAT_WINDOW_EXPANDED_SIZE.height
    });

    motion.rememberBounds({ x: 180, y: 120, width: 800, height: 600 });
    expect(motion.resolveBounds("collapsed", workArea)).toEqual({
      x: 180,
      y: 624,
      width: 96,
      height: 96
    });
    expect(motion.resolveBounds("expanded", workArea)).toEqual({
      x: 180,
      y: 120,
      width: 800,
      height: 600
    });
  });

  it("从默认左下聊天球展开时保持窗口底边锚点", () => {
    const motion = createChatWindowMotion();
    const workArea = { x: 0, y: 0, width: 1280, height: 720 };

    const collapsed = motion.resolveBounds("collapsed", workArea);
    const expanded = motion.resolveBounds("expanded", workArea);

    expect(expanded.x).toBe(collapsed.x);
    expect(expanded.y + expanded.height).toBe(collapsed.y + collapsed.height);
  });

  it("公开当前模式供桌宠判断待答气泡是否应该出现", () => {
    const motion = createChatWindowMotion();
    const workArea = { x: 0, y: 0, width: 1280, height: 720 };

    expect(motion.currentMode()).toBeNull();
    motion.resolveBounds("collapsed", workArea);
    expect(motion.currentMode()).toBe("collapsed");
    motion.resolveBounds("expanded", workArea);
    expect(motion.currentMode()).toBe("expanded");
    motion.reset();
    expect(motion.currentMode()).toBeNull();
  });
});
