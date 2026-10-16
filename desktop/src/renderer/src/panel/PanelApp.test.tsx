// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const useBootstrapStatusMock = vi.fn();
const usePanelRuntimeMock = vi.fn();

vi.mock("../bootstrap/useBootstrapStatus", () => ({
  useBootstrapStatus: (...args: unknown[]) => useBootstrapStatusMock(...args)
}));
vi.mock("./usePanelRuntime", () => ({
  usePanelRuntime: (...args: unknown[]) => usePanelRuntimeMock(...args)
}));

import { PanelApp } from "./PanelApp";

describe("PanelApp", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    (window as Window & { companionPanel?: unknown }).companionPanel = {
      reportDiagnostic: vi.fn(),
      closePanel: vi.fn(),
      openConsole: vi.fn(),
      requestModelSwitch: vi.fn(),
      onModelSwitchStateChanged: vi.fn(() => () => undefined),
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        },
        live2d: { catalog: [] }
      }
    };
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    delete (window as Window & { companionPanel?: unknown }).companionPanel;
  });

  it("bootstrap 未就绪时展示阻断页", () => {
    useBootstrapStatusMock.mockReturnValue({
      status: "blocked",
      reason: "uninitialized",
      retry: vi.fn()
    });

    act(() => root.render(<PanelApp />));

    expect(container.textContent).toContain("当前还不能进入桌宠主流程");
    expect(usePanelRuntimeMock).not.toHaveBeenCalled();
  });

  it("bootstrap 就绪后只展示人物选择面板", () => {
    useBootstrapStatusMock.mockReturnValue({ status: "ready", reason: null, retry: vi.fn() });
    usePanelRuntimeMock.mockReturnValue({
      modelCatalog: [{ id: "Haru", label: "Haru", modelPath: "/Haru.model3.json" }],
      settingsHydrated: true,
      selectedModelId: "Haru",
      pendingModelId: null,
      modelSwitchError: null,
      resetAvatarLayout: vi.fn(),
      onSelectModel: vi.fn()
    });

    act(() => root.render(<PanelApp />));

    expect(container.querySelector('[aria-label="人物切换面板"]')).toBeTruthy();
    expect(container.textContent).not.toContain("后台任务");
  });
});
