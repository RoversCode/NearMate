import { useEffect, useRef, useState } from "react";
import type { BootstrapStatus, TtsModelDescriptor, TtsSetupTask } from "../../api/contracts/bootstrap";
import type { TtsCapabilities, TtsConfiguration, TtsExecutionSelection } from "../../api/contracts/ttsSetup";
import { cancelTtsModelDownload, cancelTtsSetup, confirmTtsSetup, fetchTtsConfiguration, fetchTtsModelDownloadStatus, fetchTtsModels, fetchTtsPreview, fetchTtsSetupStatus, probeTtsCapabilities, retryTtsModelDownload, retryTtsSetup, saveTtsConfiguration, startTtsModelDownload, startTtsSetup } from "../../api/services/bootstrap";

// 模型发布和进程释放也是进行中的任务；这些阶段不能停止轮询或开放新下载。
const ACTIVE_PHASES = new Set(["queued", "download", "copy", "verify", "publish", "load", "prepare_voice", "preview", "release"]);

/**
 * 让首次配置与正式模型管理共用任务状态、恢复试听和明确提交动作。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 *   onStatusChange: 确认音色后接收完整 Bootstrap 状态。
 * Returns:
 *   面板需要的权威状态、用户草稿和明确操作。
 */
export function useTtsSetupController(baseUrl: string, onStatusChange: (status: BootstrapStatus) => void) {
  const [models, setModels] = useState<TtsModelDescriptor[]>([]);
  const [configuration, setConfiguration] = useState<TtsConfiguration | null>(null);
  const [capabilities, setCapabilities] = useState<TtsCapabilities | null>(null);
  const [selection, setSelection] = useState<TtsExecutionSelection | null>(null);
  const [modelDirectory, setModelDirectory] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [task, setTask] = useState<TtsSetupTask | null>(null);
  const [downloadTask, setDownloadTask] = useState<TtsSetupTask | null>(null);
  const [initialError, setInitialError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [busy, setBusy] = useState(true);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [hasPlayedPreview, setHasPlayedPreview] = useState(false);
  const generation = useRef(0);
  const revision = useRef(0);
  const actionInFlight = useRef(false);
  // 父进程可能先收到成功或失败结果，再释放管道；动作资格开放前仍需轮询并禁止启动新任务。
  const running = ACTIVE_PHASES.has(task?.phase ?? "") || (task?.phase === "awaiting_confirmation"
    ? !task.can_confirm : task?.can_cancel === true);
  const downloadRunning = ACTIVE_PHASES.has(downloadTask?.phase ?? "") || downloadTask?.can_cancel === true;
  const anyTaskRunning = running || downloadRunning;
  const error = actionError ?? initialError ?? pollError;

  useEffect(() => {
    const current = ++generation.current;
    actionInFlight.current = false;
    revision.current += 1;
    setBusy(true); setInitialError(null); setPollError(null); setActionError(null);
    setModels([]); setConfiguration(null); setCapabilities(null); setSelection(null); setModelDirectory(""); setSelectedModel("");
    setTask(null); setDownloadTask(null); setPreviewUrl(null); setHasPlayedPreview(false);
    void Promise.all([fetchTtsModels(baseUrl), fetchTtsConfiguration(baseUrl), fetchTtsSetupStatus(baseUrl)])
      .then(([catalog, config, status]) => {
        if (current !== generation.current) return;
        setModels(catalog.models); setConfiguration(config); setCapabilities(config.capabilities);
        setSelection(config.selection); setModelDirectory(config.model_directory);
        setSelectedModel(catalog.configured_model ?? ""); setTask(status); setDownloadTask(catalog.download_task);
      }).catch(exception => { if (current === generation.current) setInitialError(exception instanceof Error ? exception.message : String(exception)); })
      .finally(() => { if (current === generation.current) setBusy(false); });
    return () => { generation.current += 1; };
  }, [baseUrl, reloadVersion]);

  useEffect(() => {
    if (busy || !running || !task?.task_id) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const taskId = task.task_id;
    const poll = async () => {
      const requestedRevision = revision.current;
      try {
        const status = await fetchTtsSetupStatus(baseUrl);
        if (!disposed && requestedRevision === revision.current) {
          // 同一控制面的另一窗口可能已经开始新任务；当前只读快照应接管，旧试听资格不能沿用。
          if (status.task_id !== taskId) { setPreviewUrl(null); setHasPlayedPreview(false); }
          setTask(status); setPollError(null);
        }
      } catch (exception) {
        if (!disposed && requestedRevision === revision.current) setPollError(exception instanceof Error ? exception.message : String(exception));
      }
      if (!disposed) timer = setTimeout(() => void poll(), 1000);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [baseUrl, task?.task_id, running, busy]);

  useEffect(() => {
    if (busy || !downloadRunning || !downloadTask?.task_id) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const requestedRevision = revision.current;
      try {
        const status = await fetchTtsModelDownloadStatus(baseUrl);
        if (disposed || requestedRevision !== revision.current) return;
        // 文件任务结束后重读磁盘清单，不能仅凭任务完成就把模型标为已下载。
        if (!ACTIVE_PHASES.has(status.phase) && !status.can_cancel) {
          const catalog = await fetchTtsModels(baseUrl);
          if (disposed || requestedRevision !== revision.current) return;
          setModels(catalog.models);
        }
        setDownloadTask(status); setPollError(null);
      } catch (exception) {
        if (!disposed && requestedRevision === revision.current) setPollError(exception instanceof Error ? exception.message : String(exception));
      }
      if (!disposed) timer = setTimeout(() => void poll(), 1000);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [baseUrl, downloadTask?.task_id, downloadRunning, busy]);

  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);

  /**
   * 将操作期间的迟到状态挡在提交边界之外，避免取消后恢复成旧下载进度。
   * Args:
   *   operation: 用户明确触发的一次请求；提交界面状态前检查其请求资格是否仍有效。
   *   affectsVoice: 操作是否改变当前语音；纯下载失败不能清除现有试听或就绪任务。
   * Returns:
   *   请求已被服务端接纳且仍属于当前页面时为 true；失败、无效或重复动作均为 false。
   */
  async function perform(operation: (isCurrent: () => boolean) => Promise<boolean>, affectsVoice = true): Promise<boolean> {
    if (busy || actionInFlight.current) return false;
    const current = generation.current;
    const actionRevision = ++revision.current;
    const isCurrent = () => current === generation.current && actionRevision === revision.current;
    actionInFlight.current = true;
    setBusy(true); setActionError(null);
    try { return await operation(isCurrent); }
    catch (exception) {
      if (!isCurrent()) return false;
      setActionError(exception instanceof Error ? exception.message : String(exception));
      // 失败响应不代表服务端没有接纳动作；只读一次真实状态，绝不自动重复下载或确认。
      if (affectsVoice) { setTask(null); setPreviewUrl(null); setHasPlayedPreview(false); }
      try {
        const currentTask = await (affectsVoice ? fetchTtsSetupStatus(baseUrl) : fetchTtsModelDownloadStatus(baseUrl));
        if (isCurrent()) {
          if (affectsVoice) setTask(currentTask);
          else setDownloadTask(currentTask);
          setPollError(null);
        }
      } catch (readException) {
        if (isCurrent()) setPollError(readException instanceof Error ? readException.message : String(readException));
      }
      return false;
    }
    finally {
      if (isCurrent()) {
        revision.current += 1;
        actionInFlight.current = false;
        setBusy(false);
      }
    }
  }

  const configurationMatches = configuration?.confirmed === true && selection !== null &&
    configuration.selection?.onnx_provider === selection.onnx_provider && configuration.selection.llama_backend === selection.llama_backend &&
    configuration.model_directory === modelDirectory.trim();
  const isAbsoluteDirectory = /^(?:[a-zA-Z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)/.test(modelDirectory.trim());
  const selectedBackendsAvailable = selection !== null && capabilities !== null &&
    capabilities.backends.some(item => item.kind === "onnx" && item.backend === selection.onnx_provider && item.available) &&
    capabilities.backends.some(item => item.kind === "llama" && item.backend === selection.llama_backend && item.available);

  return {
    models, configuration, capabilities, selection, setSelection, modelDirectory, setModelDirectory,
    selectedModel, setSelectedModel, task, downloadTask, downloadRunning, anyTaskRunning, error, initialError, pollError, actionError, busy, running, previewUrl, hasPlayedPreview,
    reload: () => setReloadVersion(value => value + 1),
    markPreviewPlayed: () => setHasPlayedPreview(true), configurationMatches, selectedBackendsAvailable,
    canSaveConfiguration: selectedBackendsAvailable && isAbsoluteDirectory && !anyTaskRunning && !busy,
    probe: () => perform(async isCurrent => {
      if (anyTaskRunning) return false;
      const result = await probeTtsCapabilities(baseUrl);
      if (!isCurrent()) return false;
      setCapabilities(result);
      return true;
    }),
    saveConfiguration: () => perform(async isCurrent => {
      if (!selection || !isAbsoluteDirectory || anyTaskRunning) return false;
      const config = await saveTtsConfiguration(baseUrl, { schema_version: 1, selection, model_directory: modelDirectory.trim() });
      if (!isCurrent()) return false;
      // 保存后不能假定旧任务仍有效；跟进读取失败时保持未知，不能沿用旧试听或重试按钮。
      setConfiguration(config); setTask(null); setPreviewUrl(null); setHasPlayedPreview(false);
      try {
        const [status, catalog] = await Promise.all([fetchTtsSetupStatus(baseUrl), fetchTtsModels(baseUrl)]);
        if (isCurrent()) { setTask(status); setModels(catalog.models); setDownloadTask(catalog.download_task); setPollError(null); }
      } catch (exception) {
        if (isCurrent()) setPollError(exception instanceof Error ? exception.message : String(exception));
      }
      return isCurrent();
    }),
    start: (modelId = selectedModel) => perform(async isCurrent => {
      if (!modelId || !configurationMatches || anyTaskRunning) return false;
      const status = await startTtsSetup(baseUrl, modelId);
      if (!isCurrent()) return false;
      setSelectedModel(modelId); setTask(status); setPollError(null); setPreviewUrl(null); setHasPlayedPreview(false);
      return true;
    }),
    cancel: () => perform(async isCurrent => {
      if (!task?.task_id || !task.can_cancel) return false;
      const status = await cancelTtsSetup(baseUrl, task.task_id);
      if (!isCurrent()) return false;
      setTask(status); setPollError(null); setPreviewUrl(null); setHasPlayedPreview(false);
      return true;
    }),
    retry: () => perform(async isCurrent => {
      if (!task?.task_id || !task.can_retry || !configurationMatches || anyTaskRunning) return false;
      const status = await retryTtsSetup(baseUrl, task.task_id);
      if (!isCurrent()) return false;
      setTask(status); setPollError(null); setPreviewUrl(null); setHasPlayedPreview(false);
      return true;
    }),
    preview: () => perform(async isCurrent => {
      if (!task?.task_id || !task.can_confirm || !configurationMatches) return false;
      const preview = await fetchTtsPreview(baseUrl, task.task_id);
      if (!isCurrent()) return false;
      setPreviewUrl(URL.createObjectURL(preview)); setHasPlayedPreview(false);
      return true;
    }),
    confirm: () => perform(async isCurrent => {
      if (!task?.task_id || !task.can_confirm || !configurationMatches || !hasPlayedPreview || anyTaskRunning) return false;
      const status = await confirmTtsSetup(baseUrl, task.task_id);
      if (!isCurrent()) return false;
      // 确认已由服务端提交，随后读取任务失败不能把成功确认改写成失败或阻止进入聊天。
      setTask(null); setPreviewUrl(null); setHasPlayedPreview(false);
      onStatusChange(status);
      try {
        const [currentTask, catalog] = await Promise.all([fetchTtsSetupStatus(baseUrl), fetchTtsModels(baseUrl)]);
        if (isCurrent()) { setTask(currentTask); setModels(catalog.models); setPollError(null); }
      } catch (exception) {
        if (isCurrent()) setPollError(exception instanceof Error ? exception.message : String(exception));
      }
      return isCurrent();
    }),
    refreshModels: () => perform(async isCurrent => {
      const catalog = await fetchTtsModels(baseUrl);
      if (!isCurrent()) return false;
      setModels(catalog.models); setDownloadTask(catalog.download_task);
      return true;
    }, false),
    download: (modelId: string) => perform(async isCurrent => {
      if (!configurationMatches || anyTaskRunning || !models.some(model => model.model_id === modelId && model.download_available)) return false;
      const status = await startTtsModelDownload(baseUrl, modelId);
      if (!isCurrent()) return false;
      // 已有完整文件时服务端可能直接结束任务，也需要同步真实文件清单。
      if (!ACTIVE_PHASES.has(status.phase) && !status.can_cancel) {
        const catalog = await fetchTtsModels(baseUrl);
        if (!isCurrent()) return false;
        setModels(catalog.models);
      }
      setDownloadTask(status); setPollError(null);
      return true;
    }, false),
    cancelDownload: () => perform(async isCurrent => {
      if (!downloadTask?.task_id || !downloadTask.can_cancel) return false;
      const status = await cancelTtsModelDownload(baseUrl, downloadTask.task_id);
      if (!isCurrent()) return false;
      setDownloadTask(status); setPollError(null);
      return true;
    }, false),
    retryDownload: () => perform(async isCurrent => {
      if (!downloadTask?.task_id || !downloadTask.can_retry || !configurationMatches || anyTaskRunning) return false;
      const status = await retryTtsModelDownload(baseUrl, downloadTask.task_id);
      if (!isCurrent()) return false;
      setDownloadTask(status); setPollError(null);
      return true;
    }, false)
  };
}
