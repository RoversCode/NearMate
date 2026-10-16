import { LoaderCircle, RotateCcw, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { RuntimeInitializationStatus } from "./runtime-status";

/**
 * 显示无法关闭的初始化窗口，让用户看到实际阶段并阻止使用尚未就绪的功能。
 *
 * Args:
 *   runtime: Backend 确认的当前阶段。
 *   connectionError: 读取进度或重试失败的安全提示。
 *   retrying: 重试请求是否正在提交。
 *   onRetry: 重新初始化操作。
 *   onRead: 重新读取进度操作。
 * Returns:
 *   圈禁焦点并阻止背景交互的进度窗口。
 */
export function RuntimeInitializationDialog({ runtime, connectionError, retrying, onRetry, onRead }: {
  runtime: RuntimeInitializationStatus;
  connectionError: string;
  retrying: boolean;
  onRetry: () => void;
  onRead: () => void;
}): JSX.Element {
  const failed = runtime.state === "failed";
  return <Dialog open>
    <DialogContent showCloseButton={false} className="max-w-md"
      onEscapeKeyDown={event => event.preventDefault()}
      onPointerDownOutside={event => event.preventDefault()}
      onInteractOutside={event => event.preventDefault()}>
      <DialogHeader>
        <div className="mb-3 flex size-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
          {failed ? <TriangleAlert className="size-6" /> : <LoaderCircle className="size-6 animate-spin" />}
        </div>
        <DialogTitle>{failed ? "NearMate 初始化未完成" : "正在初始化 NearMate"}</DialogTitle>
        <DialogDescription>必要组件准备完成后会自动继续。</DialogDescription>
      </DialogHeader>
      <div className="rounded-lg border bg-muted/40 p-4" role="status" aria-live="polite">
        <p className="text-sm font-medium">{runtime.message}</p>
      </div>
      {connectionError && <div className="space-y-3">
        <p role="alert" className="text-sm text-destructive">{connectionError}</p>
        <Button variant="outline" onClick={onRead}>重新读取进度</Button>
      </div>}
      {failed && runtime.retryable && <Button disabled={retrying} onClick={onRetry}>
        <RotateCcw className="mr-2 size-4" />{retrying ? "正在重试…" : "重新初始化"}
      </Button>}
      <p className="text-xs leading-relaxed text-muted-foreground">准备期间请保持 NearMate 开启，完成后即可继续使用。</p>
    </DialogContent>
  </Dialog>;
}
