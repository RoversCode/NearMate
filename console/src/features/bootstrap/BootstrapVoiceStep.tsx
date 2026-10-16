import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { TtsTaskProgress, formatTtsBytes, getTtsFailureGuidance } from "./TtsTaskProgress";
import type { useTtsSetupController } from "./useTtsSetupController";

type Controller = ReturnType<typeof useTtsSetupController>;

/**
 * 展示一次语音准备的模型选择、等待和恢复操作，不同时提供两个启动入口。
 * Args:
 *   controller: 与向导其他步骤共用的语音任务控制器。
 *   onContinue: 当前试听已准备好时进入试听步骤。
 * Returns:
 *   可比较下载大小的模型选项，或当前任务的进度与恢复按钮。
 */
export function BootstrapModelStep({ controller, onContinue }: { controller: Controller; onContinue: () => void }): JSX.Element {
  const [choosingAgain, setChoosingAgain] = useState(false);
  const { task, busy, running, models, selectedModel, configurationMatches } = controller;
  const hasTask = Boolean(task?.task_id && task.phase !== "unconfigured");
  const showChoices = (!hasTask || choosingAgain) && !running;
  const model = models.find(item => item.model_id === selectedModel);
  const smallestSize = Math.min(...models.filter(item => item.download_available && item.estimated_size_bytes !== null).map(item => item.estimated_size_bytes!));
  return <section className="space-y-6" aria-label="准备语音模型">
    <div className="space-y-2"><h2 className="text-xl font-semibold">为角色准备本地语音</h2>
      <p className="text-sm leading-6 text-muted-foreground">只需下载一次。模型保存在你的电脑上，之后可以在设置中更换；请保持 NearMate 运行，直到试听准备完成。</p></div>
    {showChoices ? <>
      <fieldset className="space-y-3" disabled={busy || running}>
        <legend className="mb-3 text-sm font-medium">选择一个语音模型</legend>
        {models.map(item => <label key={item.model_id} className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition-colors ${selectedModel === item.model_id ? "border-primary bg-primary/5" : "hover:bg-muted/40"} ${!item.download_available ? "cursor-not-allowed opacity-60" : ""}`}>
          <input type="radio" name="bootstrap-voice-model" className="mt-1 accent-primary" value={item.model_id} checked={selectedModel === item.model_id} disabled={!item.download_available} onChange={() => controller.setSelectedModel(item.model_id)} />
          <span className="min-w-0 flex-1"><span className="block font-medium">{item.display_name}</span>
            <span className="mt-1 block text-sm text-muted-foreground">{item.estimated_size_bytes === null ? "下载大小尚未提供" : `需下载 ${formatTtsBytes(item.estimated_size_bytes)}`}{item.estimated_size_bytes === smallestSize ? " · 下载体积较小" : ""}</span>
            {!item.download_available ? <span className="mt-1 block text-sm text-destructive">当前安装包不提供此模型，请选择其他模型。</span> : null}
          </span>
        </label>)}
      </fieldset>
      <p className="text-sm leading-6 text-muted-foreground">不确定怎么选时，可以先试下载体积较小的选项；最终音色是否合适，由下一步试听确认。</p>
      {model ? <details className="rounded-lg border p-4 text-sm"><summary className="cursor-pointer">模型来源和兼容性说明</summary><div className="mt-3 space-y-2 break-words text-muted-foreground">
        <p>{model.verification_status}</p>{model.repository ? <p>来源：{model.repository}</p> : null}
        {model.backend_note ? <p>{model.backend_note}</p> : null}
      </div></details> : null}
      <div className="flex flex-wrap items-center justify-end gap-3">
        {choosingAgain ? <Button variant="outline" disabled={busy} onClick={() => setChoosingAgain(false)}>保留当前任务</Button> : null}
        <Button disabled={busy || !configurationMatches || !model?.download_available} onClick={() => void controller.start().then(started => { if (started) setChoosingAgain(false); })}>{busy ? "正在启动…" : "下载并准备语音"}</Button>
      </div>
    </> : task ? <>
      <div className="rounded-xl border bg-muted/20 p-5"><p className="mb-4 text-sm text-muted-foreground">{models.find(item => item.model_id === task.model_id)?.display_name ?? "当前语音模型"}</p><TtsTaskProgress task={task} /></div>
      <div className="flex flex-wrap items-center gap-3">
        {task.can_cancel ? <Button variant="outline" disabled={busy} onClick={() => void controller.cancel()}>取消准备</Button> : null}
        {task.can_retry ? <Button disabled={busy || !configurationMatches} onClick={() => void controller.retry()}>{getTtsFailureGuidance(task.error)?.retryLabel ?? "继续准备"}</Button> : null}
        {!running && !task.can_confirm ? <Button variant="outline" disabled={busy} onClick={() => setChoosingAgain(true)}>重新选择模型</Button> : null}
        {task.can_confirm ? <Button className="ml-auto" disabled={busy} onClick={onContinue}>下一步：试听声音</Button> : null}
      </div>
    </> : null}
  </section>;
}

/**
 * 读取已完成任务的试听，让用户听完后再提交语音确认。
 * Args:
 *   controller: 持有试听文件、播放资格和确认操作的共享控制器。
 *   onBack: 返回模型步骤，重新选择或处理准备问题。
 * Returns:
 *   音频试听与明确的确认入口；播放失败不会开放确认按钮。
 */
export function BootstrapPreviewStep({ controller, onBack }: { controller: Controller; onBack: () => void }): JSX.Element {
  const requestedTask = useRef<string | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const { task, busy, previewUrl, hasPlayedPreview } = controller;
  useEffect(() => {
    // 待试听只下载当前任务的一份音频，不自动播放，也不因普通状态刷新反复下载。
    if (!task?.can_confirm || !task.task_id || busy || previewUrl || requestedTask.current === task.task_id) return;
    requestedTask.current = task.task_id;
    void controller.preview();
  }, [task?.task_id, task?.can_confirm, busy, previewUrl, controller.preview]);
  return <section className="space-y-6" aria-label="试听与确认">
    <div className="space-y-2"><h2 className="text-xl font-semibold">听一听，确认角色的声音</h2><p className="text-sm leading-6 text-muted-foreground">点击下方播放，听完整段音频。声音正常且符合预期后，再确认使用；没有声音时先检查电脑音量和输出设备。</p></div>
    <div className="space-y-4 rounded-xl border bg-muted/20 p-5">
      <p className="font-medium">角色语音试听</p>
      {previewUrl ? <audio key={previewUrl} aria-label="角色语音试听" className="w-full" controls src={previewUrl}
        onEnded={() => { setPlaybackError(null); controller.markPreviewPlayed(); }}
        onError={() => setPlaybackError("试听音频未能播放，请重新加载后再试。")} /> : <p role="status" className="text-sm text-muted-foreground">{busy ? "正在读取试听音频…" : "试听音频尚未载入。"}</p>}
      {playbackError ? <p role="alert" className="text-sm text-destructive">{playbackError}</p> : null}
      {hasPlayedPreview && !playbackError ? <p role="status" className="text-sm text-success">试听已播放完成，可以确认使用。</p> : null}
      {!previewUrl || playbackError ? <Button variant="outline" disabled={busy || !task?.can_confirm} onClick={() => { setPlaybackError(null); void controller.preview(); }}>重新加载试听</Button> : null}
    </div>
    <div className="flex flex-wrap justify-between gap-3">
      <Button variant="outline" disabled={busy} onClick={onBack}>返回查看语音准备</Button>
      <Button disabled={busy || !task?.can_confirm || !controller.configurationMatches || !hasPlayedPreview || !!playbackError} onClick={() => void controller.confirm()}>{busy ? "正在确认…" : "声音没问题，完成设置"}</Button>
    </div>
    <p className="text-xs text-muted-foreground">没有自动播放属于正常情况；必须由你亲自试听并确认。</p>
  </section>;
}
