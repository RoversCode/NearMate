import { describe, expect, it } from "vitest";

import { getAvatarPresentationProfile } from "../../../shared/avatarPresentationProfile";
import { resolveAvatarModelSelection } from "./avatarModelSelection";
import type { CompanionAvatarBridge } from "./avatarBridge";

const settingsFixture = {
  show_speech_bubbles: true,
};

function createBridge(): CompanionAvatarBridge {
  return {
    applyPresentationProfile: () => undefined,
    endDrag: () => undefined,
    endResize: () => undefined,
    hoverEnter: () => undefined,
    hoverLeave: () => undefined,
    moveDrag: () => undefined,
    moveResize: () => undefined,
    onModelSwitchRequested: () => () => undefined,
    openPanel: () => undefined,
    reportDiagnostic: () => undefined,
    reportModelSwitchState: () => undefined,
    runtime: {
      controlGateway: {
        httpBaseUrl: "http://127.0.0.1:8765",
        websocketUrl: "ws://127.0.0.1:8765/ws/control"
      },
      live2d: {
        defaultModelPath: "/live2d/resources/Haru/Haru.model3.json",
        catalog: [
          {
            id: "Haru",
            label: "Haru",
            modelPath: "/live2d/resources/Haru/Haru.model3.json"
          },
          {
            id: "Hiyori",
            label: "Hiyori",
            modelPath: "/live2d/resources/Hiyori/Hiyori.model3.json"
          }
        ]
      }
    },
    startDrag: () => undefined,
    startResize: () => undefined
  };
}

describe("resolveAvatarModelSelection", () => {
  it("没有 pending 时按已提交设置解析模型路径与展示档案", () => {
    const selection = resolveAvatarModelSelection({
      avatarBridge: createBridge(),
      avatarRuntime: {
        settings: {
          ...settingsFixture,
          selected_live2d_model_id: "Hiyori"
        },
        settingsHydrated: true
      },
      pendingModelId: null
    });

    expect(selection.resolvedModelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(selection.committedSelectedModelId).toBe("Hiyori");
    expect(selection.committedPresentationModelId).toBe("Hiyori");
    expect(selection.presentationProfile).toBe(getAvatarPresentationProfile("Hiyori"));
    expect(selection.rememberedModelUnavailable).toBe(false);
  });

  it("pending 切换只改变目标模型路径，展示档案继续锚定已提交人物", () => {
    const selection = resolveAvatarModelSelection({
      avatarBridge: createBridge(),
      avatarRuntime: {
        settings: {
          ...settingsFixture,
          selected_live2d_model_id: "Haru"
        },
        settingsHydrated: true
      },
      pendingModelId: "Hiyori"
    });

    expect(selection.resolvedModelPath).toBe("/live2d/resources/Hiyori/Hiyori.model3.json");
    expect(selection.targetModelId).toBe("Hiyori");
    expect(selection.committedPresentationModelId).toBe("Haru");
    expect(selection.presentationProfile).toBe(getAvatarPresentationProfile("Haru"));
    expect(selection.rememberedModelUnavailable).toBe(false);
  });

  it("记住的人物失效时回退默认路径，并用真正展示人物的档案", () => {
    const bridge = createBridge();
    bridge.runtime.live2d.catalog = [
      {
        id: "Haru",
        label: "Haru",
        modelPath: "/live2d/resources/Haru/Haru.model3.json"
      }
    ];

    const selection = resolveAvatarModelSelection({
      avatarBridge: bridge,
      avatarRuntime: {
        settings: {
          ...settingsFixture,
          selected_live2d_model_id: "Hiyori"
        },
        settingsHydrated: true
      },
      pendingModelId: null
    });

    expect(selection.resolvedModelPath).toBe("/live2d/resources/Haru/Haru.model3.json");
    expect(selection.committedPresentationModelId).toBe("Haru");
    expect(selection.presentationProfile).toBe(getAvatarPresentationProfile("Haru"));
    expect(selection.rememberedModelUnavailable).toBe(true);
  });
});
