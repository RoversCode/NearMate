import { EmptyState } from "@/components/layout/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";
import type {
  StreamVoxConfigPayload,
  StreamVoxCreateRolePayload,
  StreamVoxPreviewRolePayload,
  StreamVoxRuntimeState
} from "../../api/contracts/streamvox";
import { type ConsoleSettings } from "../../api/contracts/settings";
import { TtsSetupPanel } from "../bootstrap/TtsSetupPanel";
import type { BootstrapStatus } from "../../api/contracts/bootstrap";
import { StreamVoxRolePanel } from "./StreamVoxRolePanel";

type StreamVoxPageProps = {
  baseUrl: string;
  bootstrapStatus: BootstrapStatus;
  onBootstrapStatusChange: (status: BootstrapStatus) => void;
  settings: ConsoleSettings | null;
  runtime: StreamVoxRuntimeState | null;
  isSaving: boolean;
  errorMessage: string | null;
  warningMessage?: string | null;
  onSaveConfig: (payload: StreamVoxConfigPayload) => Promise<void>;
  onCreateRole: (payload: StreamVoxCreateRolePayload) => Promise<void | boolean>;
  onPreviewRole: (payload: StreamVoxPreviewRolePayload) => Promise<Blob | null>;
  onDeleteRole: (roleName: string) => Promise<void>;
  onCaptureCurrentVoice: () => Promise<void>;
};

/**
 * 在正式设置页管理本地模型，并按真实语音状态开放当前角色的音色操作。
 *
 * Args:
 *   baseUrl: Backend 控制面地址。
 *   bootstrapStatus: 当前首次初始化与语音确认状态。
 *   onBootstrapStatusChange: 接收试听确认后的完整初始化状态。
 *   settings: 后端持久化设置快照。
 *   runtime: 助手子进程内 StreamVox 当前状态。
 *   isSaving: 当前是否存在 StreamVox 管理操作。
 *   errorMessage: 最近一次管理操作错误。
 *   warningMessage: 写入成功但附属状态读取失败时的提醒。
 *   onSaveConfig: 保存用户明确选定的当前音色。
 *   onCreateRole: 创建当前模型下的自定义音色。
 *   onPreviewRole: 试听当前模型下的已存在音色。
 *   onDeleteRole: 删除当前模型下的非当前音色。
 *   onCaptureCurrentVoice: 把当前运行语音写入当前激活角色卡。
 *
 * Returns:
 *   语音执行配置、模型准备、试听确认和音色管理界面。
 */
export function StreamVoxPage({
  baseUrl,
  bootstrapStatus,
  onBootstrapStatusChange,
  settings,
  runtime,
  isSaving,
  errorMessage,
  warningMessage = null,
  onSaveConfig,
  onCreateRole,
  onPreviewRole,
  onDeleteRole,
  onCaptureCurrentVoice
}: StreamVoxPageProps): JSX.Element {
  return (
    <section className="space-y-6">
      <PageHeader title="StreamVox" description="查看本机语音模型的下载情况，选择模型并管理当前角色音色。" />
      {!bootstrapStatus.voice.is_ready ? (
        <p role="status" className="rounded-lg border bg-muted/40 px-4 py-3 text-sm">
          {getVoiceAvailabilityMessage(bootstrapStatus.voice.phase)}
        </p>
      ) : null}
      {errorMessage !== null ? <p className="text-sm font-medium text-destructive">StreamVox 操作失败：{errorMessage}</p> : null}
      {warningMessage !== null ? <p className="text-sm text-warning" role="status">{warningMessage}</p> : null}
      <div className="grid items-start gap-6 xl:grid-cols-2">
        <TtsSetupPanel baseUrl={baseUrl} onStatusChange={onBootstrapStatusChange} status={bootstrapStatus} />
        {settings === null ? <EmptyState>StreamVox 设置尚未可用。</EmptyState> : <StreamVoxRolePanel
          isSaving={isSaving || !bootstrapStatus.voice.is_ready || runtime === null}
          onCaptureCurrentVoice={onCaptureCurrentVoice}
          onCreateRole={onCreateRole}
          onDeleteRole={onDeleteRole}
          onPreviewRole={onPreviewRole}
          onSaveConfig={onSaveConfig}
          runtime={bootstrapStatus.voice.is_ready ? runtime : null}
        />}
      </div>
    </section>
  );
}

/**
 * 根据 Backend 的语音阶段说明当前可用性。
 *
 * Args:
 *   phase: 当前角色语音的权威准备阶段。
 * Returns:
 *   提醒用户留在模型管理中查看进度、试听确认或修复的说明。
 */
function getVoiceAvailabilityMessage(phase: string): string {
  if (phase === "awaiting_confirmation") return "语音已准备好，请在模型管理中完成试听并确认使用。";
  if (["failed", "cancelled", "interrupted", "repair_required", "unconfigured"].includes(phase)) {
    return "当前语音尚未就绪，请在模型管理中查看原因并继续准备。";
  }
  return "语音正在准备，请在模型管理中查看进度；准备完成后试听并确认使用。";
}
