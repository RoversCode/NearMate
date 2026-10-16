import { useState } from "react";
import type { ActivityFailure } from "../../api/contracts/activities";
import { Button } from "../../components/ui/button";

type ActivityFailureDetailsProps = {
  failure: ActivityFailure;
  failureReason: string | null;
  onCopy(): Promise<boolean>;
};

/**
 * 展示结构化故障报告，并提供「复制故障详情」动作。
 *
 * Args:
 *   props: Backend 已完成脱敏的故障报告、可选具体原因与复制动作。
 *
 * Returns:
 *   阶段、错误码、摘要、具体原因、技术线索与建议组成的故障卡。
 */
export function ActivityFailureDetails({
  failure,
  failureReason,
  onCopy
}: ActivityFailureDetailsProps): JSX.Element {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  const handleCopy = async (): Promise<void> => {
    // Clipboard 失败只影响局部提示，不影响详情阅读。
    const copied = await onCopy().catch(() => false);
    setCopyState(copied ? "copied" : "failed");
    if (copied) {
      // 已复制提示短暂停留后复位，避免长期占位。
      window.setTimeout(() => setCopyState("idle"), 2000);
    }
  };

  return (
    <section className="space-y-4 rounded-lg border border-destructive/30 bg-destructive/5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">故障报告</h3>
        <div className="flex items-center gap-2">
          {copyState === "failed" ? (
            <span className="text-xs text-destructive">复制失败，请手动选择文本。</span>
          ) : null}
          {copyState === "copied" ? (
            <span className="text-xs text-muted-foreground">已复制</span>
          ) : null}
          <Button onClick={() => void handleCopy()} size="sm" variant="outline">
            复制故障详情
          </Button>
        </div>
      </div>
      <dl className="grid gap-4 text-sm sm:grid-cols-2">
        <FailureField label="失败阶段" value={`${failure.stage_label}（${failure.stage_code}）`} />
        <FailureField label="故障代码" value={failure.code} mono />
        <FailureField label="原因摘要" value={failure.summary} wide />
        {failureReason !== null ? (
          <FailureField label="具体失败原因" value={failureReason} wide />
        ) : null}
        <FailureField label="处理建议" value={failure.suggested_action} wide />
        <FailureField
          label="安全技术详情"
          value={failure.technical_detail || "没有可公开的技术线索；请把任务编号提供给开发者排查。"}
          boxed
          mono
          wide
        />
      </dl>
    </section>
  );
}

/**
 * 渲染故障报告中的单条元数据。
 *
 * Args:
 *   props: 字段名、字段值与等宽字体选项。
 *
 * Returns:
 *   故障卡定义列表中的一项。
 */
function FailureField({
  label,
  value,
  boxed = false,
  mono = false,
  wide = false
}: {
  label: string;
  value: string;
  boxed?: boolean;
  mono?: boolean;
  wide?: boolean;
}): JSX.Element {
  return (
    <div className={`space-y-1${wide ? " sm:col-span-2" : ""}`}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd
        className={
          boxed
            ? "break-all rounded-md bg-muted/60 p-3 font-mono text-xs text-muted-foreground"
            : mono
              ? "break-all font-mono text-xs"
              : "font-medium"
        }
      >
        {value}
      </dd>
    </div>
  );
}
