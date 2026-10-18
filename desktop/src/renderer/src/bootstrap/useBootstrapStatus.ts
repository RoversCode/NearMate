import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { parseBackendEvent } from "../types/backendEvents";

export type BootstrapBlockedReason = "uninitialized" | "backend_unavailable";
export type BootstrapStatus = (
  | { status: "loading"; reason: null; retry(): void }
  | { status: "ready"; reason: null; retry(): void }
  | { status: "blocked"; reason: BootstrapBlockedReason; retry(): void }) & { message?: string };
type BootstrapSnapshot = { status: "loading" | "ready" | "blocked"; reason: BootstrapBlockedReason | null; message?: string };

function readInitialBootstrapSnapshot(httpBaseUrl?: string): BootstrapSnapshot {
  return httpBaseUrl ? { status: "loading", reason: null } : { status: "blocked", reason: "backend_unavailable" };
}

function bootstrapSnapshotFromPayload(payload: unknown): BootstrapSnapshot | null {
  const parsed = parseBackendEvent({ type: "bootstrap.status_changed", payload });
  if (!parsed.ok || parsed.event.type !== "bootstrap.status_changed") return null;
  const candidate = parsed.event.payload;
  return candidate.is_initialized ? { status: "ready", reason: null } : { status: "blocked", reason: "uninitialized" };
}

function statusSocketUrl(httpBaseUrl: string): string {
  const url = new URL(httpBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws/control";
  url.search = "?scope=status";
  url.hash = "";
  return url.toString();
}

/**
 * 首次读取后通过状态 WebSocket 接收运行时初始化和 Bootstrap 变化，断线时才使用低频兜底。
 *
 * @param httpBaseUrl 后端 HTTP 地址，缺失时直接报告后端不可用。
 * @returns 当前启动状态、阻断原因和手动重试入口。
 */
export function useBootstrapStatus(httpBaseUrl?: string): BootstrapStatus {
  const normalizedHttpBaseUrl = httpBaseUrl?.replace(/\/$/, "");
  const [snapshot, setSnapshot] = useState<BootstrapSnapshot>(() => readInitialBootstrapSnapshot(normalizedHttpBaseUrl));
  const [retryToken, setRetryToken] = useState(0);
  const mountedRef = useRef(true);
  const socketConnectedRef = useRef(false);
  const requestSequenceRef = useRef(0);
  const eventVersionRef = useRef(0);
  const activeControllerRef = useRef<AbortController | null>(null);

  const probe = useCallback(async (showLoading: boolean) => {
    if (!normalizedHttpBaseUrl) {
      setSnapshot({ status: "blocked", reason: "backend_unavailable" });
      return;
    }
    const sequence = ++requestSequenceRef.current;
    const eventVersionAtStart = eventVersionRef.current;
    activeControllerRef.current?.abort();
    if (showLoading) setSnapshot(current => current.message ? current : { status: "loading", reason: null });
    const controller = new AbortController();
    activeControllerRef.current = controller;
    const timeoutId = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(`${normalizedHttpBaseUrl}/api/bootstrap/status`, { signal: controller.signal });
      if (!response.ok) throw new Error("bootstrap status request failed");
      const next = bootstrapSnapshotFromPayload(await response.json());
      if (!next) throw new Error("malformed bootstrap status");
      if (mountedRef.current && sequence === requestSequenceRef.current && eventVersionAtStart === eventVersionRef.current) {
        // Bootstrap 只有配置门禁；已有的资源进度仍以初始化状态源为准。
        setSnapshot(current => next.status === "blocked" && current.reason === "uninitialized"
          ? { ...next, message: current.message } : next);
      }
    } catch {
      // WebSocket 已更新状态时，旧 HTTP 请求的失败或超时也不能覆盖它。
      if (mountedRef.current && sequence === requestSequenceRef.current && eventVersionAtStart === eventVersionRef.current) {
        setSnapshot({ status: "blocked", reason: "backend_unavailable" });
      }
    } finally {
      window.clearTimeout(timeoutId);
      if (activeControllerRef.current === controller) activeControllerRef.current = null;
    }
  }, [normalizedHttpBaseUrl]);

  const retry = useCallback(() => setRetryToken(token => token + 1), []);

  useEffect(() => {
    mountedRef.current = true;
    void probe(true);
    return () => {
      mountedRef.current = false;
      requestSequenceRef.current += 1;
      activeControllerRef.current?.abort();
    };
  }, [probe, retryToken]);

  useEffect(() => {
    if (!normalizedHttpBaseUrl || typeof WebSocket === "undefined") return;
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let fallbackTimer: number | null = null;
    let attempt = 0;
    let everConnected = false;
    let connectionGeneration = 0;
    const retiredRuntimeIds = new Set<string>();
    let latestRuntime: { runtime_id: string; revision: number } | null = null;

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== null) return;
      const delay = Math.min(10000, 500 * 2 ** Math.min(attempt++, 5));
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };
    const connect = () => {
      if (disposed) return;
      const generation = ++connectionGeneration;
      try {
        const currentSocket = new WebSocket(statusSocketUrl(normalizedHttpBaseUrl));
        socket = currentSocket;
        const isCurrent = () => !disposed && socket === currentSocket && connectionGeneration === generation;
        currentSocket.onopen = () => {
          if (!isCurrent()) return;
          socketConnectedRef.current = true;
          everConnected = true;
          attempt = 0;
        };
        currentSocket.onmessage = event => {
          if (!isCurrent()) return;
          try {
            const parsed = parseBackendEvent(JSON.parse(String(event.data)));
            if (!parsed.ok) return;
            // 只有实际更新界面的事件才替代 HTTP 结果，阶段就绪本身不能替代完整配置门禁。
            if (parsed.event.type === "bootstrap.status_changed") {
              const next = bootstrapSnapshotFromPayload(parsed.event.payload);
              if (!next) return;
              eventVersionRef.current += 1;
              setSnapshot(current => next.status === "blocked" && current.reason === "uninitialized"
                ? { ...next, message: current.message } : next);
            } else {
              const runtime = parsed.event.type === "runtime.initialization_changed" ? parsed.event.payload : undefined;
              // 同一 Backend 的较旧快照不能重新关闭已准备好的运行时；进度 ready 仍只记录版本。
              if (runtime) {
                if (retiredRuntimeIds.has(runtime.runtime_id)) return;
                if (latestRuntime && latestRuntime.runtime_id !== runtime.runtime_id) retiredRuntimeIds.add(latestRuntime.runtime_id);
                if (latestRuntime?.runtime_id === runtime.runtime_id && runtime.revision < latestRuntime.revision) return;
                latestRuntime = runtime;
              }
              // 运行时需要清理旧资源和准备模型；进度 ready 不能代替完整 Bootstrap 放行。
              if (runtime?.state === "initializing" || runtime?.state === "failed") {
                eventVersionRef.current += 1;
                setSnapshot({ status: "blocked", reason: "uninitialized", message: runtime.message });
              } else if (runtime) {
                setSnapshot(current => ({ ...current, message: undefined }));
              }
            }
          } catch {
            return;
          }
        };
        currentSocket.onclose = () => {
          if (!isCurrent()) return;
          socketConnectedRef.current = false;
          if (!disposed) {
            if (everConnected) {
              setSnapshot(current => current.status === "ready" ? { status: "blocked", reason: "backend_unavailable" } : current);
            }
            scheduleReconnect();
          }
        };
        currentSocket.onerror = () => {
          if (isCurrent()) currentSocket.close();
        };
      } catch {
        if (!disposed && connectionGeneration === generation) {
          socketConnectedRef.current = false;
          scheduleReconnect();
        }
      }
    };
    connect();
    fallbackTimer = window.setInterval(() => {
      if (!socketConnectedRef.current) void probe(false);
    }, 60000);
    const refreshOnFocus = () => void probe(false);
    window.addEventListener("focus", refreshOnFocus);
    document.addEventListener("visibilitychange", refreshOnFocus);
    return () => {
      disposed = true;
      socketConnectedRef.current = false;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (fallbackTimer !== null) window.clearInterval(fallbackTimer);
      window.removeEventListener("focus", refreshOnFocus);
      document.removeEventListener("visibilitychange", refreshOnFocus);
      socket?.close();
    };
  }, [normalizedHttpBaseUrl, probe]);

  return useMemo<BootstrapStatus>(() => {
    if (snapshot.status === "ready") return { status: "ready", reason: null, retry };
    if (snapshot.status === "loading") return { status: "loading", reason: null, retry };
    return { status: "blocked", reason: snapshot.reason ?? "backend_unavailable", message: snapshot.message, retry };
  }, [retry, snapshot]);
}
