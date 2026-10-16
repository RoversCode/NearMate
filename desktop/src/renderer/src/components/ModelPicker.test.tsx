// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ModelPicker } from "./ModelPicker";

describe("ModelPicker", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  /**
   * 测试数据固定复用两个人物目录，避免每个用例都重复拼装同一份 catalog。
   * 这里保持与共享类型一致，只提供视图层真正关心的目录名与展示名。
   */
  const models = [
    {
      id: "Haru",
      label: "Haru",
      modelPath: "/live2d/resources/Haru/Haru.model3.json"
    },
    {
      id: "Natori",
      label: "Natori",
      modelPath: "/live2d/resources/Natori/Natori.model3.json"
    }
  ] as const;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  /**
   * renderPicker 统一处理默认渲染参数。
   * 这样每个用例只覆盖自己关心的状态差异，避免测试噪音淹没断言意图。
   */
  function renderPicker({
    selectedModelId = "Haru",
    pendingModelId = null,
    errorMessage = null,
    disabled = false,
    onSelect = vi.fn()
  }: {
    selectedModelId?: string | null;
    pendingModelId?: string | null;
    errorMessage?: string | null;
    disabled?: boolean;
    onSelect?: ReturnType<typeof vi.fn>;
  }) {
    act(() => {
      root.render(
        <ModelPicker
          models={models}
          selectedModelId={selectedModelId}
          pendingModelId={pendingModelId}
          errorMessage={errorMessage}
          disabled={disabled}
          onSelect={onSelect}
        />
      );
    });

    return { onSelect };
  }

  it("渲染目录名与当前选中态，并展示失败文案", () => {
    renderPicker({
      errorMessage: "人物资源加载失败。"
    });

    const haruButton = container.querySelector('[data-model-id="Haru"]');
    const natoriButton = container.querySelector('[data-model-id="Natori"]');
    const errorMessage = container.querySelector(".model-picker-error");

    expect(haruButton?.textContent).toContain("Haru");
    expect(haruButton?.textContent).toContain("当前");
    expect(haruButton?.getAttribute("data-selected")).toBe("true");
    expect(natoriButton?.textContent).toContain("Natori");
    expect(natoriButton?.getAttribute("data-selected")).toBe("false");
    expect(errorMessage?.textContent).toBe("人物资源加载失败。");
  });

  it("pending 时禁用所有切换，并在目标条目显示切换中", () => {
    renderPicker({
      pendingModelId: "Natori"
    });

    const buttons = Array.from(container.querySelectorAll("button")) as HTMLButtonElement[];
    const pendingButton = container.querySelector('[data-model-id="Natori"]');

    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(true);
    expect(pendingButton?.textContent).toContain("切换中");
  });

  it("点击当前模型不会重复触发 onSelect", () => {
    const onSelect = vi.fn();

    renderPicker({ onSelect });

    const currentButton = container.querySelector('[data-model-id="Haru"]') as HTMLButtonElement;

    act(() => {
      currentButton.click();
    });

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("点击非当前模型会触发 onSelect", () => {
    const onSelect = vi.fn();

    renderPicker({ onSelect });

    const targetButton = container.querySelector('[data-model-id="Natori"]') as HTMLButtonElement;

    act(() => {
      targetButton.click();
    });

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("Natori");
  });

  it("上层禁用时保持只读展示，不触发切换", () => {
    const onSelect = vi.fn();

    renderPicker({
      disabled: true,
      onSelect
    });

    const buttons = Array.from(container.querySelectorAll("button")) as HTMLButtonElement[];

    expect(buttons).toHaveLength(2);
    expect(buttons.every((button) => button.disabled)).toBe(true);

    act(() => {
      buttons[1]?.click();
    });

    expect(onSelect).not.toHaveBeenCalled();
  });
});
