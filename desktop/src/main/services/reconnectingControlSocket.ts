import {
  calculateControlReconnectDelayMs,
  CONTROL_RECONNECT_STABLE_CONNECTION_MS
} from "../../shared/controlReconnectBackoff";
import type {
  ControlSettingsSocket,
  ControlSettingsSocketHandlers
} from "./controlSettingsSocket";

type ControlSocketFactory = (
  url: string,
  handlers: ControlSettingsSocketHandlers
) => ControlSettingsSocket;

type CreateReconnectingControlSocketOptions = {
  url: string;
  socketFactory: ControlSocketFactory;
  onMessage(message: string): void;
  reconnectDelayMs?: number;
  random?: () => number;
  logger: Pick<Console, "warn">;
  logName: string;
};

export type ReconnectingControlSocket = {
  start(): void;
  stop(): void;
};

/**
 * 创建一条会自动重连、并能隔离旧连接回调的控制 WebSocket。
 *
 * @param options 地址、底层 socket 工厂、消息消费者和重连配置。
 * @returns 可随上层服务生命周期启动和停止的连接控制器。
 */
export function createReconnectingControlSocket(
  options: CreateReconnectingControlSocketOptions
): ReconnectingControlSocket {
  let started = false;
  let activeGeneration = 0;
  let socket: ControlSettingsSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let stableConnectionTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempt = 0;

  function isCurrentGeneration(generation: number): boolean {
    return started && generation === activeGeneration;
  }

  function connect(): void {
    if (!started) {
      return;
    }

    const generation = activeGeneration + 1;
    activeGeneration = generation;
    try {
      const nextSocket = options.socketFactory(options.url, {
        onOpen() {
          if (!isCurrentGeneration(generation)) {
            return;
          }
          clearStableConnectionTimer();
          stableConnectionTimer = setTimeout(() => {
            if (!isCurrentGeneration(generation)) {
              return;
            }
            reconnectAttempt = 0;
            stableConnectionTimer = null;
          }, CONTROL_RECONNECT_STABLE_CONNECTION_MS);
        },
        onMessage(message) {
          if (isCurrentGeneration(generation)) {
            options.onMessage(message);
          }
        },
        onClose() {
          scheduleReconnect(generation);
        },
        onError(error) {
          if (!isCurrentGeneration(generation)) {
            return;
          }
          options.logger.warn(`${options.logName} websocket error`, error);
          scheduleReconnect(generation);
        }
      });

      // 工厂理论上异步触发回调，但测试替身或异常实现可能同步关闭连接。
      // generation 已失效时必须立刻关闭返回句柄，不能把死连接重新发布为当前连接。
      if (!isCurrentGeneration(generation)) {
        nextSocket.close();
        return;
      }
      socket = nextSocket;
    } catch (error) {
      if (!isCurrentGeneration(generation)) {
        return;
      }
      options.logger.warn(`${options.logName} websocket connect failed`, error);
      scheduleReconnect(generation);
    }
  }

  function scheduleReconnect(generation: number): void {
    if (!isCurrentGeneration(generation)) {
      return;
    }

    // 先作废 generation，再关闭句柄。close 产生的迟到回调会因此直接退出。
    activeGeneration += 1;
    clearStableConnectionTimer();
    const closingSocket = socket;
    socket = null;
    closingSocket?.close();
    if (!started || reconnectTimer !== null) {
      return;
    }

    const delayMs = calculateControlReconnectDelayMs(reconnectAttempt, {
      initialDelayMs: options.reconnectDelayMs,
      random: options.random
    });
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delayMs);
  }

  function clearReconnectTimer(): void {
    if (reconnectTimer === null) {
      return;
    }
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  function clearStableConnectionTimer(): void {
    if (stableConnectionTimer === null) {
      return;
    }
    clearTimeout(stableConnectionTimer);
    stableConnectionTimer = null;
  }

  return {
    start(): void {
      if (started) {
        return;
      }
      started = true;
      connect();
    },

    stop(): void {
      if (!started) {
        return;
      }
      started = false;
      activeGeneration += 1;
      clearReconnectTimer();
      clearStableConnectionTimer();
      reconnectAttempt = 0;
      const closingSocket = socket;
      socket = null;
      closingSocket?.close();
    }
  };
}
