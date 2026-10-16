import { useEffect, useState, type ReactNode } from "react";

import { AppHeader } from "./components/layout/AppHeader";
import { AppSidebar } from "./components/layout/AppSidebar";
import { StatusBadge } from "./components/layout/StatusBadge";
import { Button } from "./components/ui/button";
import { TooltipProvider } from "./components/ui/tooltip";
import { BootstrapSetupPage } from "./features/bootstrap/BootstrapSetupPage";
import {
  activeSectionDescriptionById,
  type ConsoleLoadState
} from "./app/consoleTypes";
import { renderActiveSection } from "./app/renderActiveSection";
import { useConsoleActions } from "./app/useConsoleActions";
import { useConsoleData } from "./app/useConsoleData";
import { useConsoleNavigation } from "./app/useConsoleNavigation";
import { RuntimeInitializationGate } from "./features/runtime-initialization/RuntimeInitializationGate";
import { NEARMATE_CONSOLE_TITLE } from "./brand/brandIdentity";

/**
 * 渲染本地控制台应用壳层。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   根据 bootstrap 状态渲染首次初始化页或普通控制台工作台；运行时重建后保留本次向导的完成交接。
 */
export function App(): JSX.Element {
  // 运行时门禁会在运行时重建时卸载业务页面；交接意图放在这里，用户才能看完首次使用说明。
  const [onboardingActive, setOnboardingActive] = useState(false);
  return <RuntimeInitializationGate><ConsoleApplication onboardingActive={onboardingActive} onOnboardingChange={setOnboardingActive} /></RuntimeInitializationGate>;
}

/**
 * 运行时完成状态检查后挂载首次配置或日常控制台。
 *
 * Args:
 *   onboardingActive: 本次首次向导是否仍等待用户主动进入控制台。
 *   onOnboardingChange: 在应用壳层保存向导交接意图的回调。
 * Returns:
 *   首次设置、完成说明或普通管理页面；运行时阻断交给初始化门禁。
 */
function ConsoleApplication({ onboardingActive, onOnboardingChange }: {
  onboardingActive: boolean;
  onOnboardingChange: (active: boolean) => void;
}): JSX.Element {
  const baseUrl = window.location.origin;
  const navigation = useConsoleNavigation();
  const dataController = useConsoleData(baseUrl);
  const {
    consoleData,
    setConsoleData,
    bootstrapStatus,
    setBootstrapStatus,
    isConsoleAvailable,
    loadState,
    loadConsoleData,
    loadCharacterCardData
  } = dataController;
  const actions = useConsoleActions({
    baseUrl,
    consoleData,
    setConsoleData,
    setBootstrapStatus,
    loadConsoleData
  });

  useEffect(() => {
    // 只有从未确认过语音的用户才进入首次向导；既有用户切换模型时会暂时失去 is_initialized，但仍留在 StreamVox 管理页。
    if (bootstrapStatus && !isConsoleAvailable && bootstrapStatus.voice.has_confirmed_voice !== true) {
      onOnboardingChange(true);
    }
  }, [bootstrapStatus, isConsoleAvailable, onOnboardingChange]);

  useEffect(() => {
    if (
      navigation.sectionId === "character-cards" &&
      !onboardingActive &&
      bootstrapStatus?.is_initialized &&
      loadState.status === "ready"
    ) {
      void loadCharacterCardData(baseUrl);
    }
  }, [baseUrl, bootstrapStatus, loadState.status, navigation.sectionId, onboardingActive]);

  // 首屏失败后给用户明确的重试入口，避免只能手动刷新整个页面。
  const retryLoadConsoleData = (): void => {
    void loadConsoleData(baseUrl);
  };

  // bootstrap 状态是进入控制台的第一道门禁；状态未返回前只展示外壳，避免普通导航短暂暴露。
  if (bootstrapStatus === null) {
    return (
      <TooltipProvider>
        <SimpleShell
          copy="正在检查首次初始化状态，确认前不会加载普通控制台页面。"
          loadState={loadState}
          onRetry={retryLoadConsoleData}
        />
      </TooltipProvider>
    );
  }

  // 首次未初始化、LLM 失效仍回到引导；已有管理会话在准备语音时保留当前页面。
  if (!isConsoleAvailable || onboardingActive) {
    return (
      <TooltipProvider>
        <SimpleShell
          copy="跟随引导完成首次设置，让桌面角色能够与你聊天。"
          loadState={loadState}
          onRetry={retryLoadConsoleData}
        >
          <BootstrapSetupPage
            baseUrl={baseUrl}
            errorMessage={actions.bootstrapSaveErrorMessage}
            isSaving={actions.isSavingBootstrap}
            warningMessage={actions.bootstrapSaveWarningMessage}
            llmConfig={consoleData.llmConfig}
            onComplete={async () => {
              if (!bootstrapStatus.is_initialized) return;
              // 先完成整页加载再开放按需页面，避免角色卡在旧快照与新快照间重复请求。
              await loadConsoleData(baseUrl);
              onOnboardingChange(false);
            }}
            onLlmConfigSubmit={actions.handleSaveLlmConfig}
            status={bootstrapStatus}
            supportedLlmModels={consoleData.supportedLlmModels}
            onStatusChange={setBootstrapStatus}
          />
        </SimpleShell>
      </TooltipProvider>
    );
  }

  // 助手暂停期间只开放语音准备入口；恢复就绪后回到用户原先选择的页面。
  const restrictToStreamVox = !bootstrapStatus.is_initialized;
  const activeSectionId = restrictToStreamVox ? "streamvox" : navigation.sectionId;
  const activeSectionDescription = activeSectionDescriptionById[activeSectionId];

  return (
    <TooltipProvider>
      <div className="flex min-h-screen flex-col lg:flex-row">
        <AppSidebar
          activeSectionId={activeSectionId}
          onSelectSection={sectionId => navigation.navigate(sectionId)}
          restrictToStreamVox={restrictToStreamVox}
        />

        <div className="flex min-w-0 flex-1 flex-col">
          <AppHeader
            description={activeSectionDescription}
            loadState={loadState}
            title="控制台"
          />

          <main id="console-main" className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8">
            {loadState.status === "failed" ? <ErrorBanner message={loadState.errorMessage} onRetry={retryLoadConsoleData} /> : null}

            <div className="mx-auto w-full max-w-6xl">
              {renderActiveSection({
                activeSectionId,
                activityId: navigation.activityId,
                taskView: navigation.taskView,
                baseUrl,
                onNavigateActivity: activityId => navigation.navigate("tasks", activityId),
                onNavigateTaskView: taskView => navigation.navigate("tasks", null, taskView),
                dataController: {
                  ...dataController,
                  bootstrapStatus
                },
                actions
              })}
            </div>
          </main>
        </div>
      </div>
    </TooltipProvider>
  );
}

type SimpleShellProps = {
  copy: string;
  loadState: ConsoleLoadState;
  onRetry: () => void;
  children?: ReactNode;
};

/**
 * 渲染门禁态（检查中/未初始化）使用的单栏简易外壳。
 *
 * Args:
 *   copy: 外壳头部展示的说明文案。
 *   loadState: 首屏接口加载状态。
 *   onRetry: 首屏加载失败后重新发起加载的回调。
 *   children: 可选的门禁内容（如首次初始化表单）。
 *
 * Returns:
 *   居中单栏布局，包含品牌头部、状态徽标与内容区。
 */
function SimpleShell({ copy, loadState, onRetry, children }: SimpleShellProps): JSX.Element {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-4xl flex-col gap-4 px-4 py-6">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 rounded-xl border bg-card px-6 py-4 shadow-sm">
        <div className="space-y-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">{NEARMATE_CONSOLE_TITLE}</h1>
          <p className="max-w-xl text-sm leading-relaxed text-muted-foreground">{copy}</p>
        </div>
        <StatusBadge loadState={loadState} />
      </header>

      {loadState.status === "failed" ? <ErrorBanner message={loadState.errorMessage} onRetry={onRetry} /> : null}

      {children !== undefined ? (
        <div className="rounded-xl border bg-card shadow-sm">{children}</div>
      ) : null}
    </main>
  );
}

/**
 * 渲染首屏接口失败横幅。
 *
 * Args:
 *   message: 后端返回的失败原因，可能为空。
 *   onRetry: 点击重试按钮后重新发起首屏加载。
 *
 * Returns:
 *   破坏性语义的警告横幅，附重试按钮。
 */
function ErrorBanner({ message, onRetry }: { message: string | null; onRetry: () => void }): JSX.Element {
  return (
    <div className="mb-4 flex items-center justify-between gap-4 rounded-lg border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive">
      <p>加载失败：{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>重试</Button>
    </div>
  );
}
