// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AvatarListeningIndicator } from "./AvatarListeningIndicator";

describe("AvatarListeningIndicator", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean })
      .IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it("只在本地聆听激活且人物边界可用时显示于人物头侧", () => {
    act(() => {
      root.render(
        <AvatarListeningIndicator
          active={true}
          petBounds={{ x: 100, y: 40, width: 200, height: 500 }}
        />
      );
    });

    const indicator = container.querySelector<HTMLElement>("[data-avatar-listening='true']");
    expect(indicator?.style.left).toBe("260px");
    expect(indicator?.style.top).toBe("120px");
    expect(indicator?.children).toHaveLength(3);

    act(() => {
      root.render(
        <AvatarListeningIndicator
          active={false}
          petBounds={{ x: 100, y: 40, width: 200, height: 500 }}
        />
      );
    });
    expect(container.querySelector("[data-avatar-listening='true']")).toBeNull();
  });
});
