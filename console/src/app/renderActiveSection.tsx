import { CharacterCardPage } from "../features/character-cards/CharacterCardPage";
import { LogRecordsPage } from "../features/logs/LogRecordsPage";
import { SettingsPage } from "../features/settings/SettingsPage";
import { StreamVoxPage } from "../features/streamvox/StreamVoxPage";
import { EmptyState } from "../components/layout/EmptyState";
import { TaskCenterPage } from "../features/tasks/TaskCenterPage";
import { BackgroundAgentUsagePage } from "../features/background-agent-usage/BackgroundAgentUsagePage";
import { SkillLibraryPage } from "../features/skills/SkillLibraryPage";
import type { BootstrapStatus } from "../api/contracts/bootstrap";
import { consoleSections, type ConsoleSectionId, type TaskView } from "./consoleTypes";
import type { ConsoleActionsController } from "./useConsoleActions";
import type { ConsoleDataController } from "./useConsoleData";

export type RenderActiveSectionProps = {
  activeSectionId: ConsoleSectionId;
  activityId: string | null;
  taskView: TaskView;
  baseUrl: string;
  onNavigateActivity(activityId: string | null): void;
  onNavigateTaskView(taskView: TaskView): void;
  dataController: Omit<ConsoleDataController, "bootstrapStatus"> & {
    bootstrapStatus: BootstrapStatus;
  };
  actions: ConsoleActionsController;
};

/**
 * 根据当前导航选择渲染页面。
 *
 * Args:
 *   props: 控制台当前数据、保存状态和页面动作。
 *
 * Returns:
 *   当前导航对应的功能页组件。
 */
export function renderActiveSection({
  activeSectionId,
  activityId,
  taskView,
  baseUrl,
  onNavigateActivity,
  onNavigateTaskView,
  dataController,
  actions
}: RenderActiveSectionProps): JSX.Element {
  const {
    consoleData,
    bootstrapStatus,
    loadState,
    isLoadingCharacterCard,
    characterCardLoadErrorMessage,
    streamVoxLoadErrorMessage,
    audioDeviceApplyResult,
    setConsoleData,
    handleSelectCharacterCard: onSelectCharacterCard
  } = dataController;
  const {
    isSavingSettings,
    isSavingBootstrap,
    isSavingCharacterCard,
    isSavingStreamVox,
    settingsSaveErrorMessage,
    bootstrapSaveErrorMessage,
    bootstrapSaveWarningMessage,
    characterCardSaveWarningMessage,
    streamVoxSaveWarningMessage,
    handleSaveSettings: onSaveSettings,
    handleSaveCharacterCard: onSaveCharacterCard,
    handleActivateCharacterCard: onActivateCharacterCard,
    handleApplyCharacterCardVoice: onApplyCharacterCardVoice,
    handleCalibrateCharacterCardVoiceCaption: onCalibrateCharacterCardVoiceCaption,
    handleCloneCharacterCard: onCloneCharacterCard,
    handleDeleteWakeResponseItem: onDeleteWakeResponseItem,
    handleGenerateCharacterCardStreamVoxExamples: onGenerateStreamVoxExamples,
    handleGenerateWakeResponse: onGenerateWakeResponse,
    handleSynthesizeWakeResponse: onSynthesizeWakeResponse,
    handleSaveWakeResponse: onSaveWakeResponse,
    handleUploadCharacterCardCover: onUploadCharacterCardCover,
    handleUploadCharacterCardVoiceReference: onUploadCharacterCardVoiceReference,
    handleDeleteCharacterCardCover: onDeleteCharacterCardCover,
    handleSaveStreamVoxConfig: onSaveStreamVoxConfig,
    handleCreateStreamVoxRole: onCreateStreamVoxRole,
    handlePreviewStreamVoxRole: onPreviewStreamVoxRole,
    handleDeleteStreamVoxRole: onDeleteStreamVoxRole,
    handleCaptureStreamVoxVoiceToCharacterCard: onCaptureStreamVoxVoiceToCharacterCard,
    handleInitializeBootstrap: onInitializeBootstrap,
    handleSaveLlmConfig: onSaveLlmConfig,
    handleSaveQWeatherApiKey: onSaveQWeatherApiKey,
    refreshSettingsSnapshot
  } = actions;
  const isInitialLoading = loadState.status === "loading";
  const characterCardSaveErrorMessage = actions.characterCardSaveErrorMessage ?? characterCardLoadErrorMessage;
  const streamVoxSaveErrorMessage = actions.streamVoxSaveErrorMessage ?? streamVoxLoadErrorMessage;
  const characterAuthoringEnabled =
    consoleData.llmConfig?.role_bindings.character_authoring !== null &&
    consoleData.llmConfig?.status.roles.character_authoring.is_ready === true;
  const onLocalSettingsChange = (settings: NonNullable<typeof consoleData.settings>): void => {
    setConsoleData((currentData) => {
      // 音频保存和 WebSocket 都可能同时返回设置快照；不能让迟到的旧 revision
      // 把刚刚收到的更新覆盖掉，否则下一次普通设置提交又会触发 409。
      if (
        currentData.settings !== null
        && currentData.settings.settings_revision > settings.settings_revision
      ) {
        return currentData;
      }
      return {
        ...currentData,
        settings
      };
    });
  };
  if (activeSectionId === "settings") {
    return (
      <SettingsPage
        baseUrl={baseUrl}
        bootstrapSaveErrorMessage={bootstrapSaveErrorMessage}
        bootstrapSaveWarningMessage={bootstrapSaveWarningMessage}
        audioDeviceApplyResult={audioDeviceApplyResult}
        bootstrapStatus={bootstrapStatus}
        isSaving={isSavingSettings}
        isSavingBootstrap={isSavingBootstrap}
        onBootstrapSubmit={onInitializeBootstrap}
        onLocalSettingsChange={onLocalSettingsChange}
        onSettingsSnapshotRefresh={refreshSettingsSnapshot}
        onLlmConfigSubmit={onSaveLlmConfig}
        onQWeatherSubmit={onSaveQWeatherApiKey}
        onSave={onSaveSettings}
        saveErrorMessage={settingsSaveErrorMessage}
        settings={consoleData.settings}
        shortcutStatus={consoleData.shortcutStatus}
        llmConfig={consoleData.llmConfig}
        supportedLlmModels={consoleData.supportedLlmModels}
      />
    );
  }

  if (activeSectionId === "tasks") {
    return (
      <TaskCenterPage
        activityId={activityId}
        baseUrl={baseUrl}
        onNavigateActivity={onNavigateActivity}
        onNavigateTaskView={onNavigateTaskView}
        taskView={taskView}
      />
    );
  }

  if (activeSectionId === "usage") {
    return <BackgroundAgentUsagePage baseUrl={baseUrl} />;
  }

  if (activeSectionId === "skills") {
    return <SkillLibraryPage baseUrl={baseUrl} onSettingsSnapshotRefresh={refreshSettingsSnapshot} />;
  }

  if (activeSectionId === "character-cards") {
    return (
      <CharacterCardPage
        activeCard={consoleData.activeCharacterCard}
        cardsPayload={consoleData.characterCards}
        characterAuthoringEnabled={characterAuthoringEnabled}
        errorMessage={characterCardSaveErrorMessage}
        warningMessage={characterCardSaveWarningMessage}
        isLoading={isInitialLoading || isLoadingCharacterCard}
        isSaving={isSavingCharacterCard}
        onActivate={onActivateCharacterCard}
        onApplyVoice={onApplyCharacterCardVoice}
        onCalibrateVoiceCaption={onCalibrateCharacterCardVoiceCaption}
        onClone={onCloneCharacterCard}
        onDeleteCover={onDeleteCharacterCardCover}
        onDeleteWakeResponseItem={onDeleteWakeResponseItem}
        onGenerateStreamVoxExamples={onGenerateStreamVoxExamples}
        onGenerateWakeResponse={onGenerateWakeResponse}
        onSynthesizeWakeResponse={onSynthesizeWakeResponse}
        onSave={onSaveCharacterCard}
        onSaveWakeResponse={onSaveWakeResponse}
        onSelectCard={onSelectCharacterCard}
        onUploadCover={onUploadCharacterCardCover}
        onUploadVoiceReference={onUploadCharacterCardVoiceReference}
        wakeResponseConfig={consoleData.wakeResponseConfig}
      />
    );
  }

  if (activeSectionId === "streamvox") {
    return (
      <StreamVoxPage
        baseUrl={baseUrl}
        bootstrapStatus={bootstrapStatus}
        onBootstrapStatusChange={dataController.setBootstrapStatus}
        errorMessage={streamVoxSaveErrorMessage}
        warningMessage={streamVoxSaveWarningMessage}
        isSaving={isSavingStreamVox}
        onCreateRole={onCreateStreamVoxRole}
        onDeleteRole={onDeleteStreamVoxRole}
        onCaptureCurrentVoice={onCaptureStreamVoxVoiceToCharacterCard}
        onPreviewRole={onPreviewStreamVoxRole}
        onSaveConfig={onSaveStreamVoxConfig}
        runtime={consoleData.streamVoxRuntime}
        settings={consoleData.settings}
      />
    );
  }

  if (isInitialLoading) {
    const activeSection = consoleSections.find((section) => section.id === activeSectionId);

    // 运行日志依赖首屏接口；加载期间展示占位，避免把初始空数组误呈现为“暂无记录”。
    return <LoadingPanel sectionLabel={activeSection?.label ?? "控制台数据"} />;
  }

  return <LogRecordsPage rows={consoleData.logs} />;
}

/**
 * 渲染非设置页的首屏加载占位。
 *
 * Args:
 *   sectionLabel: 当前导航页签的人类可读名称。
 *
 * Returns:
 *   明确的加载态占位。
 */
function LoadingPanel({ sectionLabel }: { sectionLabel: string }): JSX.Element {
  return <EmptyState>正在加载{sectionLabel}，请稍候。</EmptyState>;
}
