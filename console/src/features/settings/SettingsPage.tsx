import { useState } from "react";
import { AudioLines, SlidersHorizontal, Cpu, UserRound } from "lucide-react";
import { cn } from "@/lib/utils";
import { ShortcutSettingsPanel } from "./ShortcutSettingsPanel";
import { UserProfileSettingsForm } from "./UserProfileSettingsForm";

import { ServiceKeysPanel } from "../bootstrap/ServiceKeysPanel";
import { LlmConfigPanel } from "../llm/LlmConfigPanel";
import { AudioDeviceSettingsPanel } from "./AudioDeviceSettingsPanel";
import { BackgroundAgentSettingsPanel } from "./BackgroundAgentSettingsPanel";
import { SpeakerVerificationSettingsForm } from "./SpeakerVerificationSettingsForm";
import { ToolWaitingPolicyPanel } from "./ToolWaitingPolicyPanel";
import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { EmptyState } from "@/components/layout/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";
import { SettingRow } from "@/components/layout/SettingRow";
import type { AudioDeviceApplyResult } from "../../api/contracts/audioDevices";
import type { BootstrapInitializePayload, BootstrapStatus, QWeatherApiKeyPayload } from "../../api/contracts/bootstrap";
import type { LlmConfig, LlmConfigUpdatePayload, SupportedLlmModelsResponse } from "../../api/contracts/llm";
import type { ConsoleSettings } from "../../api/contracts/settings";
import type { ShortcutStatusPayload } from "../../api/contracts/shortcutStatus";

type SettingsPageProps = {
  /** 控制面 HTTP 根地址；未显式传入时兼容浏览器同源部署。 */
  baseUrl?: string;
  settings: ConsoleSettings | null;
  shortcutStatus: ShortcutStatusPayload | null;
  isSaving: boolean;
  saveErrorMessage: string | null;
  audioDeviceApplyResult: AudioDeviceApplyResult | null;
  bootstrapStatus: BootstrapStatus;
  llmConfig: LlmConfig | null;
  supportedLlmModels: SupportedLlmModelsResponse | null;
  isSavingBootstrap: boolean;
  bootstrapSaveErrorMessage: string | null;
  bootstrapSaveWarningMessage?: string | null;
  onSave: (settings: ConsoleSettings) => Promise<void | boolean>;
  onLocalSettingsChange?: (settings: ConsoleSettings) => void;
  /** 专用设置接口成功后刷新全局 settings_revision 快照。 */
  onSettingsSnapshotRefresh?: (candidateSettings?: ConsoleSettings) => Promise<unknown>;
  onBootstrapSubmit: (payload: BootstrapInitializePayload) => Promise<boolean>;
  onLlmConfigSubmit: (payload: LlmConfigUpdatePayload) => Promise<boolean>;
  onQWeatherSubmit: (payload: QWeatherApiKeyPayload) => Promise<boolean>;
};

type SettingsSwitchConfig = {
  key: "show_speech_bubbles";
  title: string;
  description: string;
};

const settingsSwitches: SettingsSwitchConfig[] = [
  {
    key: "show_speech_bubbles",
    title: "语音气泡",
    description: "在头像旁显示普通语音气泡。"
  }
];

const settingsCategories = [
  { id: "general", label: "常用", icon: SlidersHorizontal },
  { id: "voice", label: "声音与语音", icon: AudioLines },
  { id: "services", label: "AI 与服务", icon: Cpu },
  { id: "profile", label: "我的信息", icon: UserRound }
];

/**
 * 渲染控制台设置页。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址；未传入时使用当前页面来源，兼容同源部署。
 *   settings: 后端当前设置；保存状态描述提醒设置和启动密钥两类提交过程；回调负责写回后端。
 *
 * Returns:
 *   按常用、声音与语音、AI 与服务、我的信息分区展示设置；
 *   设置尚未加载时展示占位说明，保存失败由父组件传入错误文本并保留当前界面状态。
 */
export function SettingsPage({
  baseUrl = window.location.origin,
  settings,
  shortcutStatus,
  isSaving,
  saveErrorMessage,
  audioDeviceApplyResult,
  bootstrapStatus,
  llmConfig,
  supportedLlmModels,
  isSavingBootstrap,
  bootstrapSaveErrorMessage,
  bootstrapSaveWarningMessage = null,
  onSave,
  onLocalSettingsChange,
  onSettingsSnapshotRefresh,
  onBootstrapSubmit,
  onLlmConfigSubmit,
  onQWeatherSubmit
}: SettingsPageProps): JSX.Element {
  const [activeCategory, setActiveCategory] = useState("general");

  if (settings === null) {
    return (
      <section className="space-y-6">
        <PageHeader title="设置" description="正在读取你的偏好设置。" />
        <EmptyState>设置尚未可用。</EmptyState>
      </section>
    );
  }

  /**
   * 切换单个设置项并立即提交。
   *
   * Args:
   *   key: 被切换的设置字段。
   *
   * Returns:
   *   无；生成新的设置对象并交给父组件保存。保存期间禁用控件，
   *   避免连续点击造成后端状态覆盖顺序不确定。
   */
  async function handleToggle(key: SettingsSwitchConfig["key"]): Promise<void> {
    if (settings === null || isSaving) {
      return;
    }

    // 新设置只修改当前开关，其余字段保持后端最近一次成功返回的状态，避免页面局部操作误覆盖其他设置。
    await onSave({
      ...settings,
      [key]: !settings[key]
    });
  }

  return (
    <section className="space-y-6">
      <PageHeader title="设置" description="让 NearMate 适合你的工作习惯。" />
      <div aria-label="设置分类" className="settings-tabs" role="tablist">
        {settingsCategories.map(({ id, label, icon: Icon }, index) => (
          <button
            aria-controls={`settings-panel-${id}`}
            aria-selected={activeCategory === id}
            className={cn("settings-tab", activeCategory === id && "settings-tab-active")}
            id={`settings-tab-${id}`}
            key={id}
            onClick={() => setActiveCategory(id)}
            onKeyDown={(event) => {
              const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
              const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? settingsCategories.length - 1
                : (index + offset + settingsCategories.length) % settingsCategories.length;
              if (!offset && event.key !== "Home" && event.key !== "End") return;
              event.preventDefault();
              const nextId = settingsCategories[nextIndex].id;
              setActiveCategory(nextId);
              document.getElementById(`settings-tab-${nextId}`)?.focus();
            }}
            role="tab"
            tabIndex={activeCategory === id ? 0 : -1}
            type="button"
          >
            <Icon aria-hidden="true" className="size-4" />{label}
          </button>
        ))}
      </div>
      {saveErrorMessage !== null ? (
        <p role="alert" className="rounded-lg border border-destructive/25 bg-destructive/5 px-4 py-3 text-sm text-destructive">保存失败：{saveErrorMessage}</p>
      ) : null}
      {bootstrapSaveWarningMessage !== null ? (
        <p role="status" className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3 text-sm text-warning">{bootstrapSaveWarningMessage}</p>
      ) : null}

      <div aria-labelledby="settings-tab-general" className="settings-panel" hidden={activeCategory !== "general"} id="settings-panel-general" role="tabpanel" tabIndex={0}>
        <ShortcutSettingsPanel saveErrorMessage={saveErrorMessage} settings={settings} shortcutStatus={shortcutStatus} isSaving={isSaving} onSave={onSave} />
        <Card>
          <CardContent className="divide-y py-1">
            {settingsSwitches.map((item) => (
              <SettingRow description={item.description} key={item.key} title={item.title}>
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground">{settings[item.key] ? "开启" : "关闭"}</span>
                  <Switch
                    aria-label={`${settings[item.key] ? "关闭" : "开启"}${item.title}`}
                    checked={settings[item.key]}
                    disabled={isSaving}
                    onCheckedChange={() => {
                      void handleToggle(item.key);
                    }}
                  />
                </div>
              </SettingRow>
            ))}
          </CardContent>
        </Card>

      </div>

      <div aria-labelledby="settings-tab-voice" className="settings-panel" hidden={activeCategory !== "voice"} id="settings-panel-voice" role="tabpanel" tabIndex={0}>
        <AudioDeviceSettingsPanel
          applyResult={audioDeviceApplyResult}
          baseUrl={baseUrl}
          onSaved={(nextSettings) => {
            onLocalSettingsChange?.(nextSettings);
          }}
          settings={settings}
        />

        <SpeakerVerificationSettingsForm
          baseUrl={baseUrl}
          isSavingSettings={isSaving}
          onSettingsSnapshotRefresh={onSettingsSnapshotRefresh}
        />

        <ToolWaitingPolicyPanel baseUrl={baseUrl} />
      </div>

      <div aria-labelledby="settings-tab-services" className="settings-panel" hidden={activeCategory !== "services"} id="settings-panel-services" role="tabpanel" tabIndex={0}>
        <BackgroundAgentSettingsPanel
          baseUrl={baseUrl}
          onSettingsChange={onLocalSettingsChange}
        />

        <LlmConfigPanel
          config={llmConfig}
          description="配置对话、记忆与任务理解使用的模型。保存并验证连接后生效。"
          errorMessage={bootstrapSaveErrorMessage}
          isSaving={isSavingBootstrap}
          onSubmit={onLlmConfigSubmit}
          supportedModels={supportedLlmModels}
          title="LLM API 配置"
        />

        <ServiceKeysPanel
          description="配置可选的天气查询服务。"
          errorMessage={bootstrapSaveErrorMessage}
          isSaving={isSavingBootstrap}
          mode="settings"
          onBootstrapSubmit={onBootstrapSubmit}
          onQWeatherSubmit={onQWeatherSubmit}
          status={bootstrapStatus}
          title="扩展服务"
        />
      </div>

      <div aria-labelledby="settings-tab-profile" className="settings-panel" hidden={activeCategory !== "profile"} id="settings-panel-profile" role="tabpanel" tabIndex={0}>
        <UserProfileSettingsForm isSaving={isSaving} onSave={onSave} settings={settings} />
      </div>
    </section>
  );
}
