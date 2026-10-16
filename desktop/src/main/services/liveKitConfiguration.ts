import { createHmac, randomBytes } from "node:crypto";

/** 只在主进程内传递的媒体服务连接配置，禁止写入日志或 renderer。 */
export type LiveKitConnectionSettings = { url: string; apiKey: string; apiSecret: string };
export type LocalLiveKitPorts = { httpPort: number; tcpPort: number; udpPort: number };

/**
 * 检查外部部署是否提供了完整配置；未提供时交给本地组件。
 *
 * @param environment 待检查的宿主环境，只读取三个 LiveKit 字段。
 * @returns 完整外部连接配置；完全未配置时返回 null。
 */
export function readExternalLiveKitConnection(environment: NodeJS.ProcessEnv): LiveKitConnectionSettings | null {
  const values = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"].map(name => {
    const matches = Object.entries(environment).filter(([key, value]) => key.toUpperCase() === name && value !== undefined);
    if (matches.length > 1) throw new Error("外部语音服务配置存在重复字段，请清理后重启 NearMate。");
    return matches[0]?.[1]?.trim() ?? "";
  });
  if (values.every(value => !value)) return null;
  if (values.some(value => !value)) throw new Error("外部语音服务配置不完整，请同时设置地址和两项凭据，或全部清除后使用内置组件。");
  const [url, apiKey, apiSecret] = values;
  try {
    const parsed = new URL(url);
    if (!["ws:", "wss:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password || parsed.search || parsed.hash) {
      throw new Error();
    }
  } catch {
    throw new Error("外部语音服务地址无效，请使用不含凭据的 ws 或 wss 地址。");
  }
  return { url, apiKey, apiSecret };
}

/**
 * 生成本机服务端点和一次性凭据，集中维护媒体候选地址配置。
 *
 * @param ports 已为本次启动选择的 HTTP、媒体 TCP 与媒体 UDP 端口。
 * @returns 服务环境变量可用的 JSON（也是合法 YAML），及 Backend 连接配置。
 */
export function createLocalLiveKitConfiguration(ports: LocalLiveKitPorts): { configBody: string; connection: LiveKitConnectionSettings } {
  const connection = {
    url: `ws://127.0.0.1:${ports.httpPort}`,
    apiKey: randomBytes(18).toString("hex"),
    apiSecret: randomBytes(32).toString("hex")
  };
  return {
    connection,
    configBody: JSON.stringify({
      port: ports.httpPort,
      bind_addresses: ["127.0.0.1"],
      rtc: {
        tcp_port: ports.tcpPort, udp_port: ports.udpPort,
        // native SDK 不支持仅 loopback 网卡的媒体候选；信令仍限于本机，媒体由 ICE 选择本机网卡。
        enable_loopback_candidate: true, use_external_ip: false
      },
      keys: { [connection.apiKey]: connection.apiSecret },
      logging: { level: "error" }
    })
  };
}

/**
 * 签发只能短时查询房间的令牌，验证响应服务持有本次随机凭据。
 *
 * @param connection 本次启动的主进程连接配置。
 * @param nowMs 当前毫秒时间，测试可以固定。
 * @returns 仅供本地 HTTP 就绪探测使用的 JWT。
 */
export function createLiveKitHealthToken(connection: LiveKitConnectionSettings, nowMs = Date.now()): string {
  const seconds = Math.floor(nowMs / 1000);
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify({ iss: connection.apiKey, sub: "nearmate-health", nbf: seconds - 5, exp: seconds + 30, video: { roomList: true } })).toString("base64url");
  const data = `${header}.${payload}`;
  return `${data}.${createHmac("sha256", connection.apiSecret).update(data).digest("base64url")}`;
}

/**
 * 只给本地服务保留 Windows 基础环境，配置不落盘也不出现在命令行。
 *
 * @param base 宿主环境。
 * @param configBody 本次服务的内存配置。
 * @returns 不包含宿主业务 Key 或开发配置的子进程环境。
 */
export function createLiveKitServerEnvironment(base: NodeJS.ProcessEnv, configBody: string): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(base)) {
    if (["SYSTEMROOT", "WINDIR", "TEMP", "TMP"].includes(key.toUpperCase()) && value !== undefined) environment[key] = value;
  }
  return { ...environment, LIVEKIT_CONFIG: configBody };
}
