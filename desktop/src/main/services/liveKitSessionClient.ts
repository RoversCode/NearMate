import {
  readLiveKitDesktopSession,
  type BackendLiveKitSessionPayload,
  type LiveKitDesktopSession
} from "./liveKitSessionPayload";
import { fetchLiveKitSessionPayload, LiveKitSessionRequestError } from "../../shared/liveKitSessionRequest";

/**
 * LiveKitSessionClient 暴露创建桌面 LiveKit session 的窄接口。
 * 核心输出是解析后的内部 session；边界异常包括 HTTP 非成功状态与后端响应结构不完整。
 */
export type LiveKitSessionClient = {
  createSession(signal?: AbortSignal): Promise<LiveKitDesktopSession>;
};

/**
 * FetchLike 描述 session client 需要的最小 fetch 能力。
 * 这样测试可以注入 fake fetch，生产环境默认使用 Electron 主进程所在 Node runtime 的 globalThis.fetch。
 */
type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * CreateLiveKitSessionClientOptions 是创建 session client 的配置。
 * httpBaseUrl 是控制服务 HTTP 根地址；fetch 是可选依赖注入入口，主要用于单元测试和未来替换传输层。
 */
type CreateLiveKitSessionClientOptions = {
  httpBaseUrl: string;
  fetch?: FetchLike;
};

/**
 * createLiveKitSessionClient 创建主进程 LiveKit session 客户端。
 * 核心入参是控制服务 HTTP 根地址与可选 fetch；预期输出是一个可复用的 createSession 接口。
 * 边界：根地址会去掉尾部斜杠，避免调用方传入 http://host/ 时拼出双斜杠路径。
 */
export function createLiveKitSessionClient(
  options: CreateLiveKitSessionClientOptions
): LiveKitSessionClient {
  /**
   * normalizedHttpBaseUrl 是当前客户端实例固定使用的控制服务根地址。
   * 在创建阶段归一化一次，可以让每次请求只关注业务路径拼接。
   */
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");

  /**
   * fetchImplementation 是实际发起 HTTP 请求的函数。
   * 优先使用测试或调用方注入的实现，生产环境回退到 Node runtime 的标准 fetch。
   */
  const fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);

  return {
    async createSession(signal?: AbortSignal): Promise<LiveKitDesktopSession> {
      /**
       * sessionEndpoint 是后端签发 LiveKit 桌面会话的固定接口。
       * 主进程统一请求它，renderer 后续只通过 IPC 获得结果，不再自行拼接这个地址。
       */
      const sessionEndpoint = `${normalizedHttpBaseUrl}/api/livekit/session`;
      const payload = await fetchLiveKitSessionPayload({ url: sessionEndpoint, fetch: fetchImplementation, signal });

      /**
       * JSON body 仍然是不可信输入。
       * 解析后交给独立校验函数，可以把 HTTP 成功但结构不完整的情况与请求失败区分开。
       */
      try {
        return readLiveKitDesktopSession(payload as BackendLiveKitSessionPayload);
      } catch {
        throw new LiveKitSessionRequestError("livekit_invalid_response");
      }
    }
  };
}
