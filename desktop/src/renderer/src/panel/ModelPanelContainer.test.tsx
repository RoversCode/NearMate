// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Live2dModelSwitchState } from "../../../shared/live2dModelTypes";
import type { CompanionPanelBridge } from "./panelBridge";
import type { BackendEvent, ReminderSettings } from "../types/events";
import { ModelPanelContainer } from "./ModelPanelContainer";

const controlSettings = vi.hoisted(() => ({
  selectedModelId: "Haru", failures: 0, strictReads: 0, stall: false,
  initialRead: null as Promise<ReminderSettings> | null,
  onEvent: null as ((event: BackendEvent) => void) | null
}));

vi.mock("../lib/controlSocket", () => ({
  ControlSocket: class {
    async fetchSettings(options?: { requireAuthoritative?: boolean }) {
      if (!options?.requireAuthoritative && controlSettings.initialRead) return controlSettings.initialRead;
      if (options?.requireAuthoritative) {
        controlSettings.strictReads += 1;
        if (controlSettings.stall) return new Promise<never>(() => undefined);
        if (controlSettings.failures > 0) {
          controlSettings.failures -= 1;
          throw new Error("HTTP 503");
        }
      }
      return { selected_live2d_model_id: controlSettings.selectedModelId, show_speech_bubbles: true };
    }
    connect(handlers: { onEvent(event: BackendEvent): void }) {
      controlSettings.onEvent = handlers.onEvent;
      return () => { controlSettings.onEvent = null; };
    }
  }
}));

describe("人物面板实际点击链", () => {
  let host: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let bridge: CompanionPanelBridge;
  let reportState: (state: Live2dModelSwitchState) => void;

  beforeEach(async () => {
    vi.useFakeTimers();
    controlSettings.selectedModelId = "Haru";
    controlSettings.failures = 0; controlSettings.strictReads = 0; controlSettings.stall = false;
    controlSettings.initialRead = null; controlSettings.onEvent = null;
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const stateListeners = new Set<(state: Live2dModelSwitchState) => void>();
    reportState = state => stateListeners.forEach(listener => listener({ ...state,
      requestId: "requestId" in state ? state.requestId : requestIdAt(-1)
    } as Live2dModelSwitchState));
    bridge = {
      reportDiagnostic: vi.fn(), closePanel: vi.fn(), requestModelSwitch: vi.fn(),
      onModelSwitchStateChanged(listener) {
        stateListeners.add(listener);
        return () => { stateListeners.delete(listener); };
      },
      runtime: {
        controlGateway: { httpBaseUrl: "http://127.0.0.1:8765", websocketUrl: "ws://127.0.0.1:8765/ws/control" },
        live2d: { catalog: ["Haru", "Natori", "Rice"].map(id => ({id, label: id, modelPath: `./live2d/resources/${id}/${id}.model3.json`})) }
      }
    };
    window.companionPanel = bridge;
    await act(async () => root.render(<ModelPanelContainer />));
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    delete window.companionPanel;
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function modelButton(id: string): HTMLButtonElement {
    return host.querySelector(`[data-model-id="${id}"]`) as HTMLButtonElement;
  }

  function requestIdAt(index: number): string | undefined {
    return (vi.mocked(bridge.requestModelSwitch).mock.calls.at(index) as unknown as [string, string] | undefined)?.[1];
  }

  it.each([
    { source: "迟到的旧人物快照", initialModelId: "Rice" },
    { source: "迟到的共享默认回退", initialModelId: "Haru" }
  ])("新设置事件先到时，$source 不能覆盖当前人物", async ({ initialModelId }) => {
    act(() => root.unmount());
    root = createRoot(host);
    let resolveInitialSettings!: (snapshot: ReminderSettings) => void;
    controlSettings.initialRead = new Promise(resolve => { resolveInitialSettings = resolve; });
    await act(async () => root.render(<ModelPanelContainer />));
    expect(modelButton("Natori").disabled).toBe(true);

    // 这个替身只覆盖 hook 水合顺序；生产完整事件合同由 ControlSocket 与真实 HTTP 联调覆盖。
    act(() => controlSettings.onEvent?.({
      type: "settings.changed", payload: { show_speech_bubbles: true, selected_live2d_model_id: "Natori" }
    } as BackendEvent));
    expect(modelButton("Natori").getAttribute("data-selected")).toBe("true");
    expect(modelButton("Natori").disabled).toBe(false);

    await act(async () => resolveInitialSettings({ show_speech_bubbles: true, selected_live2d_model_id: initialModelId }));
    expect(modelButton("Natori").getAttribute("data-selected")).toBe("true");
    expect(modelButton(initialModelId).getAttribute("data-selected")).toBe("false");
  });

  it("点击后没有收到头像接纳回执时显示原因并允许重试", async () => {
    act(() => modelButton("Natori").click());
    expect(bridge.requestModelSwitch).toHaveBeenCalledWith("Natori", expect.any(String));
    expect(modelButton("Natori").textContent).toContain("切换中");
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("没有响应");
    expect(modelButton("Rice").disabled).toBe(false);
    act(() => modelButton("Rice").click());
    expect(bridge.requestModelSwitch).toHaveBeenLastCalledWith("Rice", expect.any(String));
  });

  it("同一拍连续点击只提交首个人物，防止第二个请求被头像侧丢弃", () => {
    act(() => {
      modelButton("Natori").click();
      modelButton("Rice").click();
    });
    expect(bridge.requestModelSwitch).toHaveBeenCalledTimes(1);
    expect(bridge.requestModelSwitch).toHaveBeenCalledWith("Natori", expect.any(String));
  });

  it("接纳回执取消等待时限，失败原因放在人物列表前并恢复操作", async () => {
    act(() => modelButton("Natori").click());
    act(() => reportState({status: "switching", modelId: "Natori"}));
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(modelButton("Rice").disabled).toBe(true);
    act(() => reportState({status: "failed", modelId: "Natori", reason: "人物资源加载失败。"}));
    const alert = host.querySelector('[role="alert"]');
    const list = host.querySelector('.model-picker-list');
    expect(alert?.textContent).toBe("人物资源加载失败。");
    expect(alert?.compareDocumentPosition(list!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(modelButton("Rice").disabled).toBe(false);
  });

  it("超时后重试另一人物，旧请求的迟到结果不能影响新请求", async () => {
    act(() => modelButton("Natori").click());
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    act(() => modelButton("Rice").click());
    act(() => {
      reportState({status: "switching", modelId: "Natori"});
      reportState({status: "succeeded", modelId: "Natori"});
      reportState({status: "failed", modelId: "Natori", reason: "旧请求失败"});
    });
    expect(modelButton("Rice").textContent).toContain("切换中");
    expect(host.querySelector('[role="alert"]')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("没有响应");
    expect(modelButton("Rice").disabled).toBe(false);
  });

  it("保存成功但设置事件丢失时，读取权威设置后显示当前人物并解锁", async () => {
    act(() => modelButton("Natori").click());
    controlSettings.selectedModelId = "Natori";
    await act(async () => reportState({status: "succeeded", modelId: "Natori"}));
    expect(modelButton("Natori").getAttribute("data-selected")).toBe("true");
    expect(modelButton("Rice").disabled).toBe(false);
  });

  it("成功回执后的权威读取暂时失败时有界重试，恢复后采用真实选中值", async () => {
    act(() => modelButton("Natori").click());
    act(() => reportState({status: "switching", modelId: "Natori"}));
    controlSettings.selectedModelId = "Natori";
    controlSettings.failures = 1;
    await act(async () => reportState({status: "succeeded", modelId: "Natori"}));
    expect(modelButton("Rice").disabled).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(controlSettings.strictReads).toBe(2);
    expect(modelButton("Natori").getAttribute("data-selected")).toBe("true");
    expect(modelButton("Rice").disabled).toBe(false);
  });

  it("成功回执后持续读不到权威设置时显示同步失败并解锁重试", async () => {
    act(() => modelButton("Natori").click());
    act(() => reportState({status: "switching", modelId: "Natori"}));
    controlSettings.failures = 10;
    await act(async () => reportState({status: "succeeded", modelId: "Natori"}));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(controlSettings.strictReads).toBe(3);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("无法确认人物设置");
    expect(modelButton("Natori").getAttribute("data-selected")).toBe("false");
    expect(modelButton("Rice").disabled).toBe(false);
  });

  it("权威设置读取始终挂起时仍有总时限，不永久锁住人物列表", async () => {
    act(() => modelButton("Natori").click());
    act(() => reportState({status: "switching", modelId: "Natori"}));
    controlSettings.stall = true;
    await act(async () => reportState({status: "succeeded", modelId: "Natori"}));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("无法确认人物设置");
    expect(modelButton("Rice").disabled).toBe(false);
  });

  it("接纳超时后的迟到 switching 不会重新锁住人物列表", async () => {
    act(() => modelButton("Natori").click());
    const requestId = requestIdAt(-1);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    act(() => reportState({status: "switching", modelId: "Natori", requestId} as Live2dModelSwitchState));
    expect(modelButton("Rice").disabled).toBe(false);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("没有响应");
  });

  it("重试同一人物后，旧请求编号的 switching 和失败回执不能影响新请求", async () => {
    act(() => modelButton("Natori").click());
    const oldRequestId = requestIdAt(-1);
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    act(() => modelButton("Natori").click());
    const newRequestId = requestIdAt(-1);
    expect(newRequestId).toBeDefined();
    expect(newRequestId).not.toBe(oldRequestId);
    act(() => {
      reportState({status: "switching", modelId: "Natori", requestId: oldRequestId} as Live2dModelSwitchState);
      reportState({status: "failed", modelId: "Natori", reason: "旧请求失败", requestId: oldRequestId} as Live2dModelSwitchState);
    });
    expect(modelButton("Rice").disabled).toBe(true);
    expect(host.querySelector('[role="alert"]')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(5000));
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("没有响应");
    expect(modelButton("Rice").disabled).toBe(false);
  });
});
