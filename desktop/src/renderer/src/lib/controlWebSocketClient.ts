import type { BackendEvent, ConnectionStatus } from "../types/events";
import { parseBackendEvent } from "../types/events";
import {
  reportRendererDiagnostic,
  type RendererDiagnosticBridge
} from "./rendererDiagnostics";
import {
  calculateControlReconnectDelayMs,
  CONTROL_RECONNECT_STABLE_CONNECTION_MS
} from "../../../shared/controlReconnectBackoff";

export type ControlWebSocketHandlers = {
  onEvent(event: BackendEvent): void;
  onStatusChange(status: ConnectionStatus, reason?: string): void;
};

export type ControlWebSocketClientConfig = {
  websocketUrl: string;
  scope?: "runtime" | "status";
  connectTimeoutMs: number;
  reconnectDelayMs?: number;
  random?: () => number;
  diagnosticsBridge?: RendererDiagnosticBridge;
};

/**
 * ControlWebSocketClient 只负责真实控制 WebSocket 的生命周期。
 * 核心入参是连接配置与事件回调；预期输出是连接状态和后端事件流。
 * 边界异常：消息解析失败会被忽略，旧 socket 的迟到失败不会污染当前连接。
 */
export class ControlWebSocketClient {
  private readonly websocketUrl: string;
  private readonly scope: "runtime" | "status";
  private readonly connectTimeoutMs: number;
  private readonly reconnectDelayMs: number | undefined;
  private readonly random: (() => number) | undefined;
  private readonly diagnosticsBridge: RendererDiagnosticBridge | undefined;
  private socket: WebSocket | null = null;
  private handlers: ControlWebSocketHandlers | null = null;
  private connectTimeoutId: number | null = null;
  private reconnectTimeoutId: number | null = null;
  private stableConnectionTimeoutId: number | null = null;
  private reconnectAttempt = 0;
  private disposed = false;

  constructor(config: ControlWebSocketClientConfig) {
    this.websocketUrl = config.websocketUrl;
    this.scope = config.scope ?? "runtime";
    this.connectTimeoutMs = config.connectTimeoutMs;
    this.reconnectDelayMs = config.reconnectDelayMs;
    this.random = config.random;
    this.diagnosticsBridge = config.diagnosticsBridge;
  }

  /**
   * 建立控制层连接。
   * 后端不可达时只报告断开状态并安排重连，不制造本地演示数据。
   */
  connect(handlers: ControlWebSocketHandlers): () => void {
    this.handlers = handlers;
    this.disposed = false;
    handlers.onStatusChange("connecting");
    this.openWebSocket();

    return () => {
      this.disconnect();
    };
  }

  /**
   * 主动断开连接时，同时清掉计时器，避免卸载后继续发事件。
   */
  disconnect(): void {
    this.disposed = true;
    this.clearConnectTimeout();
    this.clearReconnectTimeout();
    this.clearStableConnectionTimeout();
    this.reconnectAttempt = 0;

    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }

    this.handlers = null;
  }

  /**
   * 创建或重建真实控制 WebSocket。
   * 业务意图是把首连和重连统一收敛到同一条状态机，避免两套分支产生行为漂移。
   */
  private openWebSocket(): void {
    if (this.disposed) {
      return;
    }

    this.clearReconnectTimeout();

    /**
     * 先尝试真实连接，保留和未来 backend 对接一致的表面行为。
     */
    try {
      const url = new URL(this.websocketUrl);
      if (this.scope === "status") url.searchParams.set("scope", "status");
      else url.search = "";
      const socket = new WebSocket(url.toString());
      let opened = false;
      // revision 只在单条连接代际与同一 runtime 内单调；新连接或新 runtime 都必须放行。
      let latestActivityRevision = -1;
      let latestActivityRuntimeId: string | null = null;
      let latestReminderRevision = -1;
      let latestReminderRuntimeId: string | null = null;

      this.socket = socket;
      this.connectTimeoutId = window.setTimeout(() => {
        if (!opened) {
          this.handleUnopenedSocketFailure(socket, "控制网关连接超时，等待后端恢复。");
        }
      }, this.connectTimeoutMs);

      socket.onopen = () => {
        opened = true;
        this.clearConnectTimeout();
        this.clearStableConnectionTimeout();
        this.stableConnectionTimeoutId = window.setTimeout(() => {
          if (this.socket === socket) {
            this.reconnectAttempt = 0;
          }
          this.stableConnectionTimeoutId = null;
        }, CONTROL_RECONNECT_STABLE_CONNECTION_MS);
        this.emitStatus("connected");
      };

      socket.onmessage = (message) => {
        const parsed = this.safeParseMessage(message.data);
        if (!parsed) {
          return;
        }
        if (parsed.type === "activities.snapshot_changed") {
          if (
            parsed.payload.runtime_id === latestActivityRuntimeId &&
            parsed.payload.revision < latestActivityRevision
          ) {
            return;
          }
          latestActivityRuntimeId = parsed.payload.runtime_id;
          latestActivityRevision = parsed.payload.revision;
        }
        if (parsed.type === "reminders.snapshot_changed") {
          if (
            parsed.payload.runtime_id === latestReminderRuntimeId
            && parsed.payload.revision < latestReminderRevision
          ) {
            return;
          }
          latestReminderRuntimeId = parsed.payload.runtime_id;
          latestReminderRevision = parsed.payload.revision;
        }
        this.handlers?.onEvent(parsed);
      };

      socket.onerror = () => {
        if (!opened) {
          this.handleUnopenedSocketFailure(socket, "控制网关连接失败，等待后端恢复。");
          return;
        }
        this.emitStatus("disconnected", "控制网关连接发生错误。");
      };

      socket.onclose = () => {
        this.clearConnectTimeout();
        this.socket = null;
        this.emitStatus("disconnected", "控制网关连接已关闭。");
        this.scheduleReconnect();
      };
    } catch {
      this.emitStatus("disconnected", "浏览器环境未能建立控制连接，等待后端恢复。");
      this.scheduleReconnect();
    }
  }

  /**
   * 处理尚未打开的 WebSocket 连接失败。
   * 核心入参是当前失败的 socket 与断开原因；预期输出是关闭失败连接、报告断开状态并安排重连。
   * 边界异常：如果失败 socket 已经不是当前 socket，只清理计时器并保持现有连接状态，避免旧连接迟到事件污染新连接。
   */
  private handleUnopenedSocketFailure(socket: WebSocket, reason: string): void {
    this.clearConnectTimeout();

    /**
     * 旧 socket 的迟到失败不能影响当前 socket。
     * 业务意图是保护快速重试时序，避免上一轮 onerror/onclose 覆盖新一轮连接。
     */
    if (this.socket !== socket) {
      return;
    }

    this.detachSocket(socket);
    this.socket = null;
    this.emitStatus("disconnected", reason);
    this.scheduleReconnect();
  }

  /**
   * detachSocket 负责把 socket 事件处理器摘除后关闭底层连接。
   * 核心入参是即将废弃的 WebSocket；预期输出是关闭连接但不触发本类的 onclose 重入逻辑。
   */
  private detachSocket(socket: WebSocket): void {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    socket.close();
  }

  /**
   * WebSocket 消息解析失败时上报脱敏原因，不记录原始帧或载荷。
   */
  private safeParseMessage(rawMessage: unknown): BackendEvent | null {
    if (typeof rawMessage !== "string") {
      this.reportProtocolDiagnostic("invalid_transport_frame", null);
      return null;
    }

    try {
      const parsed = parseBackendEvent(JSON.parse(rawMessage));
      if (!parsed.ok) {
        this.reportProtocolDiagnostic(parsed.reasonCode, parsed.eventType);
        return null;
      }
      return parsed.event;
    } catch {
      this.reportProtocolDiagnostic("invalid_json", null);
      return null;
    }
  }

  /**
   * @param reasonCode 不包含正文的稳定失败分类。
   * @param eventType 能从合法 JSON 信封中读取时保留事件名。
   */
  private reportProtocolDiagnostic(reasonCode: string, eventType: string | null): void {
    reportRendererDiagnostic(
      this.diagnosticsBridge,
      "warn",
      "control.event_rejected",
      { reason_code: reasonCode, event_type: eventType }
    );
  }

  /**
   * 连接状态通过统一出口分发。
   */
  private emitStatus(status: ConnectionStatus, reason?: string): void {
    this.handlers?.onStatusChange(status, reason);
  }

  /**
   * 连接超时只在建连阶段存在，任何成功、失败或卸载都要清理掉。
   */
  private clearConnectTimeout(): void {
    if (this.connectTimeoutId === null) {
      return;
    }

    window.clearTimeout(this.connectTimeoutId);
    this.connectTimeoutId = null;
  }

  /**
   * backend 短暂不可用时，控制链路需要自动发起下一轮真实连接。
   * 这里不制造本地事件，避免把连接失败误展示成真实业务状态。
   */
  private scheduleReconnect(): void {
    this.clearStableConnectionTimeout();
    if (this.disposed || this.reconnectTimeoutId !== null) {
      return;
    }

    const delayMs = calculateControlReconnectDelayMs(this.reconnectAttempt, {
      initialDelayMs: this.reconnectDelayMs,
      random: this.random
    });
    this.reconnectAttempt += 1;
    this.reconnectTimeoutId = window.setTimeout(() => {
      this.reconnectTimeoutId = null;
      if (this.disposed || this.handlers === null) {
        return;
      }

      this.emitStatus("connecting");
      this.openWebSocket();
    }, delayMs);
  }

  /**
   * 重连定时器只允许存在一份。
   * 任何成功连接或主动卸载都必须清掉，避免后台留下重复建连任务。
   */
  private clearReconnectTimeout(): void {
    if (this.reconnectTimeoutId === null) {
      return;
    }

    window.clearTimeout(this.reconnectTimeoutId);
    this.reconnectTimeoutId = null;
  }

  /** 清理稳定连接计时器，避免旧 socket 在新连接建立后重置退避次数。 */
  private clearStableConnectionTimeout(): void {
    if (this.stableConnectionTimeoutId === null) {
      return;
    }

    window.clearTimeout(this.stableConnectionTimeoutId);
    this.stableConnectionTimeoutId = null;
  }
}
