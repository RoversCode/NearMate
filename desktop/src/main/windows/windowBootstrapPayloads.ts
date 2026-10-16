import { loadLive2dModelCatalog } from "../live2d/modelCatalog";
import {
  resolveControlHttpRootUrl,
  resolveControlWebSocketUrl
} from "../../shared/controlHttpUrl";
import { resolveLive2dModelPath } from "../../shared/live2dModelPath";
import {
  buildWindowBootstrapArgument,
  type WindowBootstrapPayload
} from "../../shared/windowBootstrap";
import type { WindowKind } from "../contracts/windowKinds";

/**
 * createWindowBootstrapArguments 在 main 进程准备三个 sandbox preload 的只读启动配置。
 *
 * Returns:
 *   与 avatar、panel、chat 一一对应的 Electron additionalArgument。
 */
export function createWindowBootstrapArguments(): Record<WindowKind, string> {
  const controlGateway = {
    httpBaseUrl: resolveControlHttpRootUrl(process.env["COMPANION_CONTROL_HTTP_URL"]),
    websocketUrl: resolveControlWebSocketUrl(process.env["COMPANION_CONTROL_HTTP_URL"])
  };
  const live2dCatalog = loadLive2dModelCatalog();
  const payloads: Record<WindowKind, WindowBootstrapPayload> = {
    avatar: {
      kind: "avatar",
      runtime: {
        controlGateway,
        live2d: {
          defaultModelPath: resolveLive2dModelPath(process.env["COMPANION_LIVE2D_MODEL_PATH"]),
          catalog: live2dCatalog
        }
      }
    },
    panel: {
      kind: "panel",
      runtime: {
        controlGateway,
        live2d: {
          catalog: live2dCatalog
        }
      }
    },
    chat: {
      kind: "chat",
      runtime: {
        controlGateway
      }
    }
  };

  return {
    avatar: buildWindowBootstrapArgument(payloads.avatar),
    panel: buildWindowBootstrapArgument(payloads.panel),
    chat: buildWindowBootstrapArgument(payloads.chat)
  };
}
