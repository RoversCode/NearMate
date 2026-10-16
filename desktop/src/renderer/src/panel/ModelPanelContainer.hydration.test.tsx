// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelPanelContainer } from "./ModelPanelContainer";
import type { CompanionPanelBridge } from "./panelBridge";

describe("人物面板真实设置水合", () => {
  let host: HTMLDivElement | undefined;
  let root: ReturnType<typeof createRoot> | undefined;

  afterEach(() => {
    if (root) act(() => root?.unmount());
    host?.remove();
    delete window.companionPanel;
    vi.unstubAllGlobals();
  });

  it.each(["请求失败", "空快照"])("首次设置%s时仍能点击另一人物并收到显式切换回执", async failure => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.stubGlobal("fetch", failure === "请求失败" ? vi.fn().mockRejectedValue(new Error("网络不可达"))
      : vi.fn().mockResolvedValue(Response.json({})));
    vi.stubGlobal("WebSocket", class {
      close() {}
    });
    const requestModelSwitch = vi.fn();
    const listeners = new Set<Parameters<CompanionPanelBridge["onModelSwitchStateChanged"]>[0]>();
    window.companionPanel = {
      reportDiagnostic: vi.fn(), closePanel: vi.fn(), requestModelSwitch,
      onModelSwitchStateChanged(listener) {
        listeners.add(listener);
        return () => { listeners.delete(listener); };
      },
      runtime: {
        controlGateway: { httpBaseUrl: "http://127.0.0.1:8765", websocketUrl: "ws://127.0.0.1:8765/ws/control" },
        live2d: { catalog: ["Haru", "Natori"].map(id => ({ id, label: id, modelPath: `./live2d/resources/${id}/${id}.model3.json` })) }
      }
    };
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root?.render(<ModelPanelContainer />));
    const target = host.querySelector('[data-model-id="Natori"]') as HTMLButtonElement;
    expect(target.disabled).toBe(false);
    act(() => target.click());
    expect(requestModelSwitch).toHaveBeenCalledWith("Natori", expect.any(String));
    const requestId = requestModelSwitch.mock.calls[0][1] as string;
    act(() => listeners.forEach(listener => listener({ status: "failed", modelId: "Natori", requestId, reason: "人物设置正在加载，请稍后重试。" })));
    expect(target.disabled).toBe(false);
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("人物设置正在加载");
  });
});
