// @vitest-environment jsdom

import { afterEach, describe, expect, it } from "vitest";
import { getPanelBridge, getPanelModelCatalog } from "./panelBridge";

describe("panelBridge", () => {
  afterEach(() => {
    delete window.companionPanel;
  });

  it("缺少 preload bridge 时返回 null，并让模型目录安全兜底为空数组", () => {
    delete window.companionPanel;

    expect(getPanelBridge()).toBeNull();
    expect(getPanelModelCatalog(null)).toEqual([]);
  });

  it("存在 preload bridge 时返回原始对象，并读取 runtime 内的模型目录", () => {
    const catalog = [
      {
        id: "Haru",
        label: "Haru",
        modelPath: "/live2d/resources/Haru/Haru.model3.json"
      }
    ];
    window.companionPanel = {
      closePanel() {},
      reportDiagnostic() {},
      requestModelSwitch() {},
      onModelSwitchStateChanged() {
        return () => undefined;
      },
      runtime: {
        controlGateway: {
          websocketUrl: "ws://127.0.0.1:8765/ws/control",
          httpBaseUrl: "http://127.0.0.1:8765"
        },
        live2d: {
          catalog
        }
      }
    };

    const bridge = getPanelBridge();

    expect(bridge).toBe(window.companionPanel);
    expect(getPanelModelCatalog(bridge)).toBe(catalog);
  });
});
