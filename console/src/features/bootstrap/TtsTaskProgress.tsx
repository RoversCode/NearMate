import type { TtsSetupTask } from "../../api/contracts/bootstrap";

const PHASE_LABELS: Record<string, string> = { idle: "等待下载", completed: "模型下载完成", unconfigured: "等待配置", queued: "准备开始", download: "下载语音模型", copy: "整理已有模型", verify: "检查文件是否完整", publish: "保存已验证的模型", load: "首次加载模型", prepare_voice: "准备角色音色", preview: "生成一段试听", release: "正在结束当前任务", awaiting_confirmation: "语音已准备好，可以试听了", ready: "语音已就绪", failed: "语音准备未完成", cancelled: "已取消准备", interrupted: "上次准备被中断", repair_required: "语音配置需要重新确认" };
const PHASE_HELP: Record<string, string> = {
  queued: "任务正在启动，请保持 NearMate 运行。",
  download: "下载速度取决于网络。取消后再次准备会复用可用的下载缓存。",
  copy: "正在整理本地文件，这一步不需要重新下载完整模型。",
  verify: "正在逐个检查已下载文件。大模型需要一些时间，这不是卡在下载进度。",
  publish: "正在完成模型文件保存，请稍候。",
  load: "正在把模型加载到内存或显存。首次加载可能较慢。",
  prepare_voice: "正在用当前角色的参考声音准备音色，请稍候。",
  preview: "即将生成试听音频；完成后可以亲自听一听再确认。",
  release: "正在安全释放资源，结束后会自动显示试听或重试按钮。",
  cancelled: "可用的下载缓存已保留，准备好后可以继续。",
  interrupted: "上次进程已经结束。可以继续准备，不必手动删除已下载的文件。"
};

/**
 * 把技术故障转换为用户能采取行动的提示，原始原因仍在详情里保留。
 * Args:
 *   error: 服务端返回的安全错误文本。
 *   taskKind: 当前是语音准备还是纯文件下载。
 * Returns:
 *   故障说明和重试按钮文案；没有错误时返回 null。
 */
export function getTtsFailureGuidance(error: string | null, taskKind: "voice" | "download" = "voice"): { title: string; help: string; retryLabel: string } | null {
  if (!error) return null;
  if (/No module named|ModuleNotFoundError|DLL load failed|ImportError/i.test(error)) return {
    title: "安装组件缺失或无法加载",
    help: "请先更新或修复 NearMate 安装包，再回来重试。重复下载语音模型不能解决组件缺失；请保留已下载的模型文件。",
    retryLabel: "已更新安装包，重新尝试"
  };
  if (/WinError\s*(5|32|33)|PermissionError|Permission denied|拒绝访问/i.test(error)) return {
    title: "暂时无法保存本地文件", help: "请关闭可能占用相关文件的程序后重试。持续失败时，展开详细原因查看无法写入的位置。", retryLabel: "重新尝试"
  };
  if (/not enough space|insufficient.*space|no space left|空间不足/i.test(error)) return {
    title: "磁盘空间不足", help: "请释放磁盘空间，或返回“检查电脑”更换模型保存位置后继续。", retryLabel: "已释放空间，继续准备"
  };
  if (/timeout|timed out|connection|network|网络|连接/i.test(error)) return {
    title: "连接中断，暂时没能完成", help: "请检查网络后继续准备。已经验证可用的下载缓存会被复用。", retryLabel: "继续准备"
  };
  return { title: taskKind === "download" ? "模型下载遇到问题" : "语音准备遇到问题", help: "可以重试当前任务。若同样的错误再次出现，请将下方详细原因提供给支持人员。", retryLabel: "重新尝试" };
}

/**
 * 只在下载阶段展示网络传输数字，其它阶段说明当前实际工作。
 * Args:
 *   task: Backend 的权威语音任务状态。
 *   taskKind: 区分纯文件下载与语音准备的终态文案。
 * Returns:
 *   阶段、下载字节与错误说明。
 */
export function TtsTaskProgress({ task, taskKind = "voice" }: { task: TtsSetupTask; taskKind?: "voice" | "download" }): JSX.Element {
  const progress = task.progress;
  const downloading = task.phase === "download" && progress?.phase === "download";
  const fraction = progress?.fraction;
  const failure = getTtsFailureGuidance(task.error, taskKind);
  const downloadPhaseLabels: Record<string, string> = { failed: "模型下载未完成", cancelled: "已取消下载", interrupted: "上次下载被中断" };
  const phaseLabel = (taskKind === "download" ? downloadPhaseLabels[task.phase] : undefined) ?? PHASE_LABELS[task.phase] ?? task.phase;
  return <div className="space-y-2" aria-live="polite">
    <p className="font-medium">{phaseLabel}</p>
    {PHASE_HELP[task.phase] ? <p className="text-sm leading-6 text-muted-foreground">{PHASE_HELP[task.phase]}</p> : null}
    {downloading ? <>
      <progress aria-label="语音模型下载进度" className="h-2 w-full accent-primary" max={1} value={typeof fraction === "number" ? Math.min(1, Math.max(0, fraction)) : progress.total_bytes && progress.total_bytes > 0 ? Math.min(1, progress.completed_bytes / progress.total_bytes) : undefined} />
      <p className="text-sm">已下载 {formatTtsBytes(progress.completed_bytes)} / {progress.total_bytes == null ? "总大小尚未返回" : formatTtsBytes(progress.total_bytes)}{typeof fraction === "number" ? `（${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%）` : ""}</p>
      {progress.current_file ? <p className="break-all text-xs text-muted-foreground">{progress.current_file}</p> : null}
    </> : null}
    {task.phase === "repair_required" ? <p className="text-sm text-muted-foreground">先重新检测执行方式；缺少安装组件时，在 NearMate 启动检查窗口修复，再重试语音准备。</p> : null}
    {failure ? <div role="alert" className="space-y-2 rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm">
      <p className="font-medium text-destructive">{failure.title}</p><p className="leading-6">{failure.help}</p>
      <details><summary className="cursor-pointer text-muted-foreground">查看详细原因</summary><p className="mt-2 whitespace-pre-wrap break-words font-mono text-xs">{task.error}</p></details>
    </div> : null}
    {task.actual_backends ? <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">实际运行信息</summary><p className="mt-2">{Object.entries(task.actual_backends).map(([key, value]) => `${key}=${value}`).join("，")}</p></details> : null}
  </div>;
}

/**
 * 按真实字节数显示易读大小，不猜测未知模型的体积。
 * Args:
 *   bytes: 服务端返回的字节数。
 * Returns:
 *   带单位的文件大小。
 */
export function formatTtsBytes(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${Math.max(0, bytes)} 字节`;
}
