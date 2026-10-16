/**
 * DEFAULT_CONTROL_HTTP_ROOT_URL 是桌面端默认控制服务 HTTP 根地址。
 * 环境变量缺失时主进程和 preload 都使用同一默认值，避免两侧默认契约漂移。
 */
const DEFAULT_CONTROL_HTTP_ROOT_URL = "http://127.0.0.1:8765";

/**
 * resolveControlHttpRootUrl 负责把 COMPANION_CONTROL_HTTP_URL 解释为本机控制服务根地址。
 * 核心入参是环境变量原始值；预期输出是不带末尾斜杠的 loopback HTTP Origin。
 * 边界：空值回退到本地默认服务；非 HTTP、非 loopback、带凭据或带业务路径的地址会直接报错，避免 renderer 被配置到外部控制面。
 */
export function resolveControlHttpRootUrl(rawUrl?: string): string {
  const candidate = new URL(rawUrl?.trim() || DEFAULT_CONTROL_HTTP_ROOT_URL);

  if (candidate.protocol !== "http:") {
    throw new Error("控制服务必须使用本机 HTTP 地址");
  }

  if (candidate.hostname !== "127.0.0.1" && candidate.hostname !== "[::1]") {
    throw new Error("控制服务必须绑定 loopback 地址");
  }

  if (candidate.username || candidate.password || candidate.search || candidate.hash) {
    throw new Error("控制服务地址不能包含凭据、查询参数或片段");
  }

  if (candidate.pathname !== "/" && candidate.pathname !== "/console" && candidate.pathname !== "/console/") {
    throw new Error("控制服务地址不能包含自定义业务路径");
  }

  return candidate.origin;
}

/**
 * resolveControlWebSocketUrl 从同一个本机控制 Origin 派生只读通知 WebSocket。
 *
 * Args:
 *   rawHttpUrl: 可选的控制 HTTP 环境变量原始值。
 *
 * Returns:
 *   与 HTTP 根地址同主机、同端口的 `/ws/control` 地址。
 */
export function resolveControlWebSocketUrl(rawHttpUrl?: string): string {
  const controlHttpUrl = new URL(resolveControlHttpRootUrl(rawHttpUrl));
  controlHttpUrl.protocol = "ws:";
  controlHttpUrl.pathname = "/ws/control";
  return controlHttpUrl.toString();
}

/**
 * resolveControlConsoleUrl 负责基于控制服务 HTTP 根地址生成最终 console 页面地址。
 * 核心入参同样是环境变量原始值；预期输出固定指向 /console 页面。
 * 边界：所有根地址归一化规则委托给 resolveControlHttpRootUrl，确保主进程打开页面与 preload 暴露根地址使用同一套语义。
 */
export function resolveControlConsoleUrl(rawUrl?: string): string {
  /**
   * console 是控制服务根地址下的固定页面入口。
   * 先解析根地址再追加页面路径，可以兼容误填 /console 的历史环境变量。
   */
  return `${resolveControlHttpRootUrl(rawUrl)}/console`;
}
