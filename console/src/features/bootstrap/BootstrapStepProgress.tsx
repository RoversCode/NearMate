import { NEARMATE_HELP_LINKS } from "@/brand/helpLinks";

/** 首次引导使用统一的五步定义，保证名称、页码和教程一致。 */
export const BOOTSTRAP_STEPS = [
  { label: "连接 DeepSeek", helpUrl: NEARMATE_HELP_LINKS.firstRun.deepseek },
  { label: "连接 Qwen", helpUrl: NEARMATE_HELP_LINKS.firstRun.qwen },
  { label: "检查电脑", helpUrl: NEARMATE_HELP_LINKS.firstRun.checkComputer },
  { label: "准备语音", helpUrl: NEARMATE_HELP_LINKS.firstRun.voiceModel },
  { label: "试听并开始", helpUrl: NEARMATE_HELP_LINKS.firstRun.preview }
] as const;

/**
 * 展示当前首次初始化步骤，只展示配置进度。
 *
 * Args:
 *   activeStepIndex: 从零开始的当前步骤；全部完成时传入步骤总数。
 * Returns:
 *   可被辅助技术读取的五步进度条。
 */
export function BootstrapStepProgress({ activeStepIndex }: { activeStepIndex: number }): JSX.Element {
  return <ol aria-label="初始化步骤" className="grid grid-cols-3 gap-3 sm:grid-cols-5 sm:gap-2">
    {BOOTSTRAP_STEPS.map((step, index) => <li key={step.label}
      aria-current={index === activeStepIndex ? "step" : undefined}
      className={`space-y-2 text-center text-xs leading-5 ${index === activeStepIndex ? "font-semibold text-primary" : "text-muted-foreground"}`}>
      <span className={`block h-1 rounded-full ${index <= activeStepIndex ? "bg-primary" : "bg-muted"}`} />
      <span>{index < activeStepIndex ? "✓ " : ""}{step.label}</span>
    </li>)}
  </ol>;
}
