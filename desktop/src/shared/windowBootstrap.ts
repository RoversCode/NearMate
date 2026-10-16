import type { Live2dModelCatalog } from "./live2dModelTypes";

const WINDOW_BOOTSTRAP_ARGUMENT_PREFIX = "--companion-window-bootstrap=";

type ControlGatewayBootstrap = {
  httpBaseUrl: string;
  websocketUrl: string;
};

export type AvatarWindowBootstrap = {
  kind: "avatar";
  runtime: {
    controlGateway: ControlGatewayBootstrap;
    live2d: {
      defaultModelPath: string;
      catalog: Live2dModelCatalog;
    };
  };
};

type PanelWindowBootstrap = {
  kind: "panel";
  runtime: {
    controlGateway: ControlGatewayBootstrap;
    live2d: {
      catalog: Live2dModelCatalog;
    };
  };
};

type ChatWindowBootstrap = {
  kind: "chat";
  runtime: {
    controlGateway: ControlGatewayBootstrap;
  };
};

export type WindowBootstrapPayload =
  | AvatarWindowBootstrap
  | PanelWindowBootstrap
  | ChatWindowBootstrap;

/**
 * buildWindowBootstrapArgument 把主进程准备好的只读窗口配置编码成 Electron 启动参数。
 *
 * Args:
 *   payload: 只包含目标窗口真正需要的控制地址、模型目录或静态资源路径。
 *
 * Returns:
 *   可直接放入 webPreferences.additionalArguments 的单个参数。
 */
export function buildWindowBootstrapArgument(payload: WindowBootstrapPayload): string {
  return `${WINDOW_BOOTSTRAP_ARGUMENT_PREFIX}${encodeURIComponent(JSON.stringify(payload))}`;
}

/**
 * readWindowBootstrapArgument 从当前 preload 的进程参数中读取目标窗口配置。
 *
 * Args:
 *   argv: Electron 传给 preload 的进程参数。
 *   expectedKind: 当前 preload 所属的窗口类型。
 *
 * Returns:
 *   找到且窗口类型匹配时返回配置；缺失或损坏时返回 null。
 */
export function readWindowBootstrapArgument<TKind extends WindowBootstrapPayload["kind"]>(
  argv: readonly string[],
  expectedKind: TKind
): Extract<WindowBootstrapPayload, { kind: TKind }> | null {
  const rawArgument = argv.find(argument => argument.startsWith(WINDOW_BOOTSTRAP_ARGUMENT_PREFIX));

  if (!rawArgument) {
    return null;
  }

  try {
    const decodedPayload = JSON.parse(
      decodeURIComponent(rawArgument.slice(WINDOW_BOOTSTRAP_ARGUMENT_PREFIX.length))
    ) as unknown;

    if (!isWindowBootstrapPayload(decodedPayload) || decodedPayload.kind !== expectedKind) {
      return null;
    }

    return decodedPayload as Extract<WindowBootstrapPayload, { kind: TKind }>;
  } catch {
    return null;
  }
}

/**
 * isWindowBootstrapPayload 只校验跨进程边界必须具备的最小结构。
 * 主进程仍是这份参数的唯一生产者，具体字段由各 preload 的静态类型继续约束。
 */
function isWindowBootstrapPayload(payload: unknown): payload is WindowBootstrapPayload {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const kind = (payload as { kind?: unknown }).kind;
  return kind === "avatar" || kind === "panel" || kind === "chat";
}
