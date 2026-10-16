import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { BootstrapStatus } from "../../api/contracts/bootstrap";
import { TtsExecutionPanel } from "./TtsExecutionPanel";
import { TtsModelLibrary } from "./TtsModelLibrary";
import { TtsTaskProgress, getTtsFailureGuidance } from "./TtsTaskProgress";
import { useTtsSetupController } from "./useTtsSetupController";

type TtsSetupPanelProps = {
  baseUrl: string;
  status: BootstrapStatus;
  onStatusChange: (status: BootstrapStatus) => void;
};

/**
 * 在正式设置页管理本地语音，和首次向导共用任务控制器。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 *   status: 当前 Bootstrap 门禁状态。
 *   onStatusChange: 确认音色后接收完整门禁状态。
 * Returns:
 *   本地语音执行配置与受管模型准备面板。
 */
export function TtsSetupPanel({ baseUrl, status, onStatusChange }: TtsSetupPanelProps): JSX.Element {
  const controller = useTtsSetupController(baseUrl, onStatusChange);
  const [startingModelId, setStartingModelId] = useState<string | null>(null);
  const [playbackFailed, setPlaybackFailed] = useState(false);
  const { models, task, downloadTask, busy, anyTaskRunning, configurationMatches } = controller;
  const currentModelId = status.voice.is_ready ? status.voice.model_id : null;
  const currentModel = models.find(item => item.model_id === currentModelId);
  const startingModel = models.find(item => item.model_id === startingModelId);
  const startModelPreparation = (modelId: string): void => {
    // 后端需要先回收旧语音运行时；请求返回前没有任务快照，先把这段等待明确告诉用户。
    setStartingModelId(modelId);
    void controller.start(modelId).finally(() => setStartingModelId(null));
  };
  return <Card>
    <CardHeader>
      <CardTitle>语音模型</CardTitle>
      <CardDescription>下载其他模型时，现有语音可以继续使用。下载完成后，选择“准备并试听”来切换语音。</CardDescription>
    </CardHeader>
    <CardContent className="space-y-5">
      <div className="rounded-lg bg-muted/50 px-4 py-3 text-sm">
        <p>当前语音模型：{currentModel?.display_name ?? (currentModelId || "尚未确认")}。</p>
        <p className="mt-1 text-muted-foreground">当前角色：{status.voice.role_name ?? "尚未绑定"}。</p>
      </div>
      <section className="space-y-3" aria-label="模型管理">
        <div className="flex items-center justify-between gap-3">
          <h3 className="font-medium">模型库</h3>
          <Button type="button" variant="ghost" disabled={busy} onClick={() => void controller.refreshModels()}>刷新下载状态</Button>
        </div>
        <TtsModelLibrary models={models} currentModelId={currentModelId} downloadTask={downloadTask} downloadRunning={controller.downloadRunning}
          disabled={busy || anyTaskRunning || !configurationMatches} onDownload={modelId => void controller.download(modelId)} onPrepare={startModelPreparation} />
        {!configurationMatches ? <p className="text-xs text-muted-foreground">请先检测并确认执行方式与模型目录。</p> : null}
      </section>
      {startingModelId !== null && (!task || task.phase === "ready" || task.phase === "unconfigured") ? <section className="space-y-2 rounded-md border border-primary/30 bg-primary/5 p-4" aria-label="语音任务启动中">
        <p role="status" className="text-sm font-medium">正在退出当前语音并准备{startingModel?.display_name ?? "新模型"}，请稍候…</p>
        <p className="text-xs text-muted-foreground">NearMate 正在安全释放旧语音资源。页面会继续留在 StreamVox，准备任务开始后显示详细进度。</p>
      </section> : null}
      {downloadTask && downloadTask.phase !== "idle" ? <section className="space-y-3 rounded-md border p-4" aria-label="模型下载任务">
        <h3 className="font-medium">模型下载</h3>
        <TtsTaskProgress task={downloadTask} taskKind="download" />
        <div className="flex flex-wrap gap-2">
          {downloadTask.can_cancel ? <Button type="button" variant="outline" disabled={busy} onClick={() => void controller.cancelDownload()}>取消下载</Button> : null}
          {downloadTask.can_retry ? <Button type="button" variant="outline" disabled={busy || anyTaskRunning || !configurationMatches} onClick={() => void controller.retryDownload()}>继续下载</Button> : null}
        </div>
      </section> : null}
      {task && task.phase !== "unconfigured" && task.phase !== "ready" ? <section className="space-y-3 rounded-md border p-4" aria-label="语音任务">
        <h3 className="font-medium">准备与试听确认</h3>
        <TtsTaskProgress task={task} />
        <div className="flex flex-wrap gap-2">
          {task.can_cancel ? <Button type="button" variant="outline" disabled={busy} onClick={() => void controller.cancel()}>{task.phase === "awaiting_confirmation" ? "放弃这次试听" : "取消准备"}</Button> : null}
          {task.can_retry && task.task_id ? <Button type="button" variant="outline" disabled={busy || !configurationMatches} onClick={() => void controller.retry()}>{getTtsFailureGuidance(task.error)?.retryLabel ?? "重新尝试"}</Button> : null}
          {task.can_confirm ? <>
            <Button type="button" variant="outline" disabled={busy || !configurationMatches} onClick={() => void controller.preview().then(loaded => { if (loaded) setPlaybackFailed(false); })}>加载并恢复试听</Button>
            <Button type="button" disabled={busy || anyTaskRunning || !configurationMatches || !controller.hasPlayedPreview || playbackFailed} onClick={() => void controller.confirm()}>确认并使用这套音色</Button>
          </> : null}
        </div>
        {controller.previewUrl ? <audio key={controller.previewUrl} aria-label="角色语音试听" controls src={controller.previewUrl} onEnded={() => { setPlaybackFailed(false); controller.markPreviewPlayed(); }} onError={() => setPlaybackFailed(true)} className="w-full" /> : null}
        {playbackFailed ? <p role="alert" className="text-sm text-destructive">试听音频未能播放，请重新加载后再试。</p> : null}
        {task.can_confirm && !controller.hasPlayedPreview ? <p className="text-xs text-muted-foreground">听完整段试听后，再确认是否使用这套音色。</p> : null}
      </section> : null}
      {controller.error ? <p role="alert" className="text-sm text-destructive">{controller.error}</p> : null}
      {controller.initialError || (controller.pollError && !anyTaskRunning) ? <Button variant="outline" disabled={busy} onClick={controller.reload}>重新读取进度</Button> : null}
      <details className="rounded-md border p-4" open={!configurationMatches}>
        <summary className="cursor-pointer text-sm font-medium">保存位置与运行设置</summary>
        <div className="mt-4"><TtsExecutionPanel controller={controller} /></div>
      </details>
    </CardContent>
  </Card>;
}


