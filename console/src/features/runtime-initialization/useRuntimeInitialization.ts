import { useCallback, useEffect, useRef, useState } from "react";
import { parseRuntimeInitializationStatus, type RuntimeInitializationStatus } from "./runtime-status";

/**
 * 管理真实初始化进度、断线重取和单次重试，只依据 Backend 的真实准备进度。
 *
 * Args:
 *   enabled: 是否启用初始化状态读取与 HTTP 兜底。
 * Returns:
 *   当前快照、连接提示、重试状态，以及供状态连接接纳快照的方法。
 */
export function useRuntimeInitialization(enabled: boolean) {
  const [runtime, setRuntime] = useState<RuntimeInitializationStatus | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [retrying, setRetrying] = useState(false);
  const current = useRef<RuntimeInitializationStatus | null>(null);
  const retiredRuntimeIds = useRef(new Set<string>());
  const version = useRef(0);
  const mounted = useRef(false);
  const reading = useRef(false);
  const retryInFlight = useRef(false);
  const connected = useRef(false);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const requests = useRef(new Set<AbortController>());

  const accept = useCallback((next: RuntimeInitializationStatus) => {
    const previous = current.current;
    // Backend 重启可以从零开始计数；已经退出的进程不能靠迟到响应重新接管页面。
    if (retiredRuntimeIds.current.has(next.runtime_id) || previous &&
        previous.runtime_id === next.runtime_id && next.revision < previous.revision) return;
    if (previous?.runtime_id === next.runtime_id && previous.revision === next.revision) {
      setConnectionError("");
      return;
    }
    if (previous && previous.runtime_id !== next.runtime_id) retiredRuntimeIds.current.add(previous.runtime_id);
    version.current += 1;
    current.current = next;
    setRuntime(next);
    setConnectionError("");
  }, []);

  const read = useCallback(async () => {
    if (reading.current || !mounted.current || !enabledRef.current) return;
    reading.current = true;
    const versionAtStart = version.current;
    const controller = new AbortController();
    requests.current.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/runtime/initialization", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("初始化进度暂时不可用。");
      const next = parseRuntimeInitializationStatus(await response.json());
      if (mounted.current && versionAtStart === version.current) accept(next);
    } catch {
      // 断线不能伪造完成；保留最后阶段，等状态连接恢复或用户重新读取。
      if (mounted.current && versionAtStart === version.current) {
        setConnectionError("暂时无法读取初始化进度。正在重新连接本机服务。");
      }
    } finally {
      window.clearTimeout(timeout);
      requests.current.delete(controller);
      reading.current = false;
    }
  }, [accept]);

  const retry = useCallback(async () => {
    if (retryInFlight.current || !current.current?.retryable) return;
    retryInFlight.current = true;
    setRetrying(true);
    setConnectionError("");
    const versionAtStart = version.current;
    const controller = new AbortController();
    requests.current.add(controller);
    const timeout = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch("/api/runtime/initialization/retry", { method: "POST", signal: controller.signal });
      if (!response.ok) throw new Error("初始化重试未完成。");
      const next = parseRuntimeInitializationStatus(await response.json());
      if (mounted.current && versionAtStart === version.current) accept(next);
    } catch {
      if (mounted.current && versionAtStart === version.current) {
        setConnectionError("未能确认重试结果，请重新读取进度后再试。");
        void read();
      }
    } finally {
      window.clearTimeout(timeout);
      requests.current.delete(controller);
      retryInFlight.current = false;
      if (mounted.current) setRetrying(false);
    }
  }, [accept, read]);

  const setConnected = useCallback((value: boolean) => {
    connected.current = value;
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      for (const controller of requests.current) controller.abort();
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    void read();
    const timer = window.setInterval(() => { if (!connected.current) void read(); }, 60000);
    return () => window.clearInterval(timer);
  }, [enabled, read]);

  return { runtime, connectionError, retrying, accept, read, retry, setConnected };
}
