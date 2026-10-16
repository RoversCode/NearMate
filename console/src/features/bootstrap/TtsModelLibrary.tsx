import { Button } from "@/components/ui/button";
import type { TtsModelDescriptor, TtsSetupTask } from "../../api/contracts/bootstrap";
import { formatTtsBytes } from "./TtsTaskProgress";

type TtsModelLibraryProps = {
  models: TtsModelDescriptor[];
  currentModelId: string | null;
  downloadTask: TtsSetupTask | null;
  downloadRunning: boolean;
  disabled: boolean;
  onDownload: (modelId: string) => void;
  onPrepare: (modelId: string) => void;
};

const LOCAL_STATE_LABELS = {
  unknown: "下载状态待确认", not_downloaded: "未下载", incomplete: "下载未完成",
  downloaded: "已下载", unavailable: "当前版本不可下载"
};

/**
 * 按本机文件事实展示模型，分开下载和切换当前角色语音。
 * Args:
 *   props: 模型清单、当前语音模型、文件任务状态和用户操作。
 * Returns:
 *   每个模型的下载状态与明确操作按钮。
 */
export function TtsModelLibrary({ models, currentModelId, downloadTask, downloadRunning, disabled, onDownload, onPrepare }: TtsModelLibraryProps): JSX.Element {
  return <div className="grid gap-3 sm:grid-cols-2" aria-label="语音模型库">
    {models.map(model => {
      const localState = model.local_state ?? "unknown";
      const isCurrent = model.model_id === currentModelId;
      const isDownloading = downloadRunning && downloadTask?.model_id === model.model_id;
      const size = model.total_size_bytes ?? model.estimated_size_bytes;
      return <article key={model.model_id} aria-label={model.display_name} className={`flex flex-col gap-3 rounded-lg border p-4 ${isCurrent ? "border-primary/50 bg-primary/5" : "bg-background"}`}>
        <div className="flex items-start justify-between gap-2">
          <h3 className="font-medium">{model.display_name}</h3>
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${localState === "downloaded" ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{isDownloading ? "下载中" : LOCAL_STATE_LABELS[localState]}</span>
        </div>
        <p className="text-sm text-muted-foreground">{size == null ? "大小待确认" : formatTtsBytes(size)}</p>
        <div className="mt-auto">
          {isCurrent ? <Button type="button" variant="outline" disabled>当前使用</Button>
            : localState === "downloaded" ? <Button type="button" variant="outline" aria-label={`准备并试听 ${model.display_name}`} disabled={disabled} onClick={() => onPrepare(model.model_id)}>准备并试听</Button>
              : <Button type="button" variant="outline" aria-label={`下载 ${model.display_name}`} disabled={disabled || model.download_available === false || localState === "unknown" || localState === "unavailable"} onClick={() => onDownload(model.model_id)}>{isDownloading ? "正在下载" : localState === "incomplete" ? "继续下载" : "下载模型"}</Button>}
        </div>
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">模型信息</summary>
          <div className="mt-2 space-y-1 break-words">
            {model.repository ? <p>来源：{model.repository}</p> : null}
            {model.backend_note ? <p>{model.backend_note}</p> : null}
            <p>文件完整性会在准备语音前再次校验。</p>
          </div>
        </details>
      </article>;
    })}
  </div>;
}
