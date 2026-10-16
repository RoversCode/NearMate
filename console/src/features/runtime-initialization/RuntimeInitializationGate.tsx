import { useEffect, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { connectControlSocket } from "@/lib/controlSocket";
import { parseRuntimeInitializationStatus } from "./runtime-status";
import { useRuntimeInitialization } from "./useRuntimeInitialization";
import { RuntimeInitializationDialog } from "./RuntimeInitializationDialog";

/**
 * 在必要运行时尚未装配完成时阻止业务页面挂载。
 *
 * Args:
 *   children: idle 或 ready 状态下可继续使用的配置或控制台页面。
 * Returns:
 *   本机连接提示、不可关闭的初始化窗口或业务页面。
 */
export function RuntimeInitializationGate({ children }: { children: ReactNode }): JSX.Element {
  const { runtime, connectionError, retrying, accept, read, retry, setConnected } = useRuntimeInitialization(true);

  useEffect(() => {
    const connection = connectControlSocket(window.location.origin, {
      onOpen() { setConnected(true); void read(); },
      onClose() { setConnected(false); void read(); },
      onMessage(message) {
        if (message.type !== "runtime.initialization_changed") return;
        try {
          accept(parseRuntimeInitializationStatus(message.payload));
        } catch {
          // 不完整事件无法证明组件已经就绪，重新读取 Backend 的完整快照。
          void read();
        }
      }
    }, "status");
    const refresh = () => { void read(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      connection.close();
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [accept, read, setConnected]);

  if (runtime === null) return <main className="flex min-h-screen items-center justify-center p-6">
    <section className="w-full max-w-xl space-y-4 rounded-xl border bg-card p-6">
      <h1 className="text-xl font-semibold">正在连接 NearMate</h1>
      <p role={connectionError ? "alert" : "status"} className="text-sm text-muted-foreground">
        {connectionError || "正在检查本机服务和初始化进度，请稍候…"}
      </p>
      {connectionError && <Button variant="outline" onClick={() => void read()}>重新连接</Button>}
    </section>
  </main>;

  // idle 表示配置尚未齐备，必须允许用户继续向导；真实准备和失败才阻断页面。
  if (runtime.state === "initializing" || runtime.state === "failed") return <div className="min-h-screen bg-background">
    <header className="border-b px-6 py-5"><p className="text-lg font-semibold">NearMate</p>
      <p className="mt-1 text-sm text-muted-foreground">正在准备你的桌面伙伴</p></header>
    <RuntimeInitializationDialog runtime={runtime} connectionError={connectionError} retrying={retrying}
      onRetry={() => void retry()} onRead={() => void read()} />
  </div>;

  return <>{children}</>;
}
