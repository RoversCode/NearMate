import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { BootstrapStatus } from "../../api/contracts/bootstrap";
import type { LlmConfig, LlmConfigUpdatePayload, SupportedLlmModelsResponse } from "../../api/contracts/llm";
import { LlmConfigPanel } from "../llm/LlmConfigPanel";
import { BootstrapModelStep, BootstrapPreviewStep } from "./BootstrapVoiceStep";
import { TtsExecutionPanel } from "./TtsExecutionPanel";
import { useTtsSetupController } from "./useTtsSetupController";
import { BOOTSTRAP_STEPS, BootstrapStepProgress } from "./BootstrapStepProgress";
import { NEARMATE_HELP_LINKS } from "@/brand/helpLinks";

/** 首次引导只编排步骤，模型配置与语音资格仍由各自服务判断。 */
type BootstrapSetupPageProps = {
  baseUrl: string;
  status: BootstrapStatus;
  llmConfig: LlmConfig | null;
  supportedLlmModels: SupportedLlmModelsResponse | null;
  isSaving: boolean;
  errorMessage: string | null;
  warningMessage?: string | null;
  onLlmConfigSubmit: (payload: LlmConfigUpdatePayload) => Promise<boolean>;
  onStatusChange: (status: BootstrapStatus) => void;
  onComplete: () => void | Promise<void>;
};

/**
 * 一次只引导用户完成一件事，并从服务端已保存的进度恢复。
 *
 * Args:
 *   props: 启动状态、共享模型配置及保存和完成回调。
 * Returns:
 *   可返回修改的分步引导；服务端就绪后保留完成页，等待用户主动进入控制台。
 */
export function BootstrapSetupPage({ baseUrl, status, llmConfig, supportedLlmModels, isSaving,
  errorMessage, warningMessage = null, onLlmConfigSubmit, onStatusChange, onComplete }: BootstrapSetupPageProps): JSX.Element {
  const controller = useTtsSetupController(baseUrl, onStatusChange);
  const [step, setStep] = useState<number | null>(null);
  const [enteringConsole, setEnteringConsole] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    // 只在首次读取时恢复进度。后台每秒更新任务不能把用户从正在查看的上一步拉走。
    if (step !== null || !llmConfig || !supportedLlmModels) return;
    if (!status.llm.is_ready) {
      // 重开页面只恢复已保存配置；已经保存第一家的用户可直接补第二家，未提交的 Key 不缓存。
      const deepseek = llmConfig.credentials.deepseek;
      setStep(deepseek?.api_key_configured && deepseek.base_url.trim() ? 1 : 0);
      return;
    }
    if (!controller.configuration || !controller.task) return;
    setStep(!controller.configuration.confirmed ? 2 : controller.task.can_confirm ? 4 : 3);
  }, [step, llmConfig, supportedLlmModels, status.llm.is_ready, controller.configuration, controller.task]);

  useEffect(() => {
    // 配置资格发生变化时必须回到缺失步骤，不能靠前端页码继续使用旧的试听。
    if (step === null || isSaving) return;
    if (step > 1 && !status.llm.is_ready) setStep(1);
    else if (step > 2 && controller.configuration && !controller.configurationMatches) setStep(2);
    else if (step === 4 && controller.task && !controller.task.can_confirm && !status.is_initialized) setStep(3);
  }, [step, isSaving, status.llm.is_ready, status.is_initialized, controller.configuration, controller.configurationMatches, controller.task]);

  useEffect(() => { heading.current?.focus(); }, [step, status.is_initialized]);

  const waitingForVoice = step === null || step > 1;
  const activeStep = status.is_initialized ? BOOTSTRAP_STEPS.length : step ?? (status.llm.is_ready ? 2 : 0);
  const locked = isSaving || controller.busy || controller.running;

  return <section className="space-y-6 p-5 sm:p-7" aria-label="首次初始化">
    <header className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h1 ref={heading} tabIndex={-1} className="text-lg font-semibold outline-none">{status.is_initialized ? "设置完成" : `第 ${activeStep + 1} 步，共 ${BOOTSTRAP_STEPS.length} 步`}</h1>
        <span className="text-xs text-muted-foreground">{status.is_initialized ? "欢迎使用 NearMate" : "已保存的步骤会自动保留"}</span>
      </div>
      <BootstrapStepProgress activeStepIndex={activeStep} />
      {activeStep > 1 && activeStep < BOOTSTRAP_STEPS.length ? <a className="inline-block text-sm text-primary underline underline-offset-4" href={BOOTSTRAP_STEPS[activeStep].helpUrl} rel="noopener noreferrer" target="_blank">查看本步完整教程</a> : null}
    </header>

    {warningMessage !== null ? <p className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3 text-sm text-warning" role="status">{warningMessage}</p> : null}

    {status.is_initialized ? <section className="space-y-5 py-4" aria-label="初始化完成">
      <h2 className="text-2xl font-semibold">准备好了，开始认识你的伙伴吧</h2>
      <p className="text-sm leading-7 text-muted-foreground">把鼠标移到桌面角色上，点击旁边的“聊天”气泡图标，就能打开聊天窗口，发送第一条消息。控制台用于管理角色、声音和偏好设置，以后可以随时回来调整。</p>
      <div className="space-y-3 rounded-xl border p-4 text-sm leading-7">
        <p>先发一句“你好，介绍一下你能怎么陪我工作”，确认角色能够回应。想用语音时，先到控制台“设置 → 声音与语音”检查麦克风，并确认 Windows 音量和输出设备，再使用唤醒或跟进模式开始说话。</p>
        <div className="flex flex-wrap gap-x-4 gap-y-2"><a className="text-primary underline underline-offset-4" href={NEARMATE_HELP_LINKS.firstMessage} rel="noopener noreferrer" target="_blank">发送第一条消息</a><a className="text-primary underline underline-offset-4" href={NEARMATE_HELP_LINKS.voice} rel="noopener noreferrer" target="_blank">语音使用入门</a></div>
      </div>
      <div className="space-y-2 rounded-xl bg-muted/40 p-4 text-sm"><p>✓ AI 服务已连接</p><p>{status.voice.is_ready ? "✓ 角色语音已试听确认" : "语音尚未就绪，可在设置中继续准备。"}</p></div>
      <p className="text-sm text-muted-foreground">天气查询是可选功能，需要时再到设置中配置。<a className="ml-2 text-primary underline underline-offset-4" href={NEARMATE_HELP_LINKS.optionalServices} rel="noopener noreferrer" target="_blank">了解可选服务</a></p>
      <Button disabled={enteringConsole} onClick={() => {
        setEnteringConsole(true);
        void Promise.resolve(onComplete()).finally(() => setEnteringConsole(false));
      }}>{enteringConsole ? "正在进入控制台…" : "进入控制台"}</Button>
    </section> : <>
      {/* 返回上一步时保留尚未提交的输入；密钥只留在组件内存，不写浏览器存储。 */}
      <div hidden={step !== 0 && step !== 1}>
        <LlmConfigPanel config={llmConfig} supportedModels={supportedLlmModels} mode="onboarding"
          onboardingProvider={step === 0 ? "deepseek" : "qwen"}
          onNext={() => setStep(1)} onBack={() => setStep(0)}
          title={step === 0 ? "连接 DeepSeek，让角色与你对话" : "连接 Qwen，让角色能查资料和理解任务"}
          description={step === 0 ? "先准备一家服务的 API Key，完成后再继续。" : "填好第二家的信息后，一起验证并保存两家配置。"}
          errorMessage={errorMessage} isSaving={isSaving || controller.running} submitLabel="验证两家连接并继续"
          onSubmit={async payload => {
            const saved = await onLlmConfigSubmit(payload);
            if (saved) setStep(2);
            return saved;
          }} />
        {status.llm.is_ready ? <div className="mt-4 flex justify-end"><Button variant="outline" disabled={locked} onClick={() => setStep(2)}>使用已保存的 AI 配置继续</Button></div> : null}
      </div>

      {waitingForVoice && controller.initialError ? <div role="alert" className="space-y-3 rounded-lg border border-destructive/30 p-4 text-sm">
        <p>暂时无法读取语音准备进度，已保存的配置不会被清空。</p><p className="break-words text-muted-foreground">{controller.initialError}</p><Button variant="outline" disabled={controller.busy} onClick={controller.reload}>重新读取进度</Button>
      </div> : waitingForVoice && !controller.configuration ? <p role="status" className="py-8 text-sm text-muted-foreground">正在读取已保存的配置…</p> : null}

      {step !== null && step > 1 && controller.configuration ? <>
        {controller.error ? <div role="alert" className="space-y-2 rounded-lg border border-destructive/30 p-4 text-sm">
          <p>{controller.pollError && !controller.actionError ? "暂时无法更新进度，正在尝试重新连接。" : "这次操作未完成，请根据提示重试。"}</p><p className="break-words text-muted-foreground">{controller.error}</p>
          {controller.pollError && !controller.running ? <Button variant="outline" disabled={controller.busy} onClick={controller.reload}>重新读取进度</Button> : null}
        </div> : null}
        {step === 2 ? <TtsExecutionPanel controller={controller} guided onContinue={() => setStep(3)} /> : null}
        {step === 3 ? <BootstrapModelStep controller={controller} onContinue={() => setStep(4)} /> : null}
        {step === 4 ? <BootstrapPreviewStep controller={controller} onBack={() => setStep(3)} /> : null}
      </> : null}

      {step === 2 || step === 3 ? <footer className="flex flex-wrap items-center gap-3 border-t pt-4">
        <Button variant="ghost" disabled={locked} onClick={() => setStep(step - 1)}>上一步：{BOOTSTRAP_STEPS[step - 1].label}</Button>
        {controller.running ? <p className="text-xs text-muted-foreground">语音正在准备中，结束或取消后可返回修改配置。</p> : <p className="text-xs text-muted-foreground">返回查看不会更改已保存的配置。</p>}
      </footer> : null}
    </>}
    <footer className="text-xs text-muted-foreground">遇到问题？<a className="ml-2 text-primary underline underline-offset-4" href={NEARMATE_HELP_LINKS.troubleshooting} rel="noopener noreferrer" target="_blank">查看常见问题与排查方法</a></footer>
  </section>;
}
