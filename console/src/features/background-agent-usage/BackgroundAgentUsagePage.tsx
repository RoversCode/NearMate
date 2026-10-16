import { useEffect, useMemo, useState } from "react";

import type { BackgroundAgentUsageResponse } from "@/api/contracts/backgroundAgentUsage";
import { fetchBackgroundAgentUsage } from "@/api/services/backgroundAgentUsage";
import { EmptyState } from "@/components/layout/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { UsageByModelTable } from "./UsageByModelTable";
import { UsageDailyTrend } from "./UsageDailyTrend";
import { UsageSummaryCards } from "./UsageSummaryCards";
import {
  presetUsageDateRange,
  validateUsageDateRange,
  type UsageDateRange,
  type UsageRangePreset
} from "./usageDateRange";
import { useCurrentLocalDate } from "./useCurrentLocalDate";

type RequestState = "loading" | "ready" | "failed";

/**
 * 按需加载并展示后台工作长期用量。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *
 * Returns:
 *   日期筛选、总览、模型表和每日趋势组成的独立页面。
 */
export function BackgroundAgentUsagePage({ baseUrl }: { baseUrl: string }): JSX.Element {
  const [preset, setPreset] = useState<UsageRangePreset>("today");
  const [dateRange, setDateRange] = useState<UsageDateRange>(() =>
    presetUsageDateRange("today")
  );
  const [requestState, setRequestState] = useState<RequestState>("loading");
  const [report, setReport] = useState<BackgroundAgentUsageResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [retrySequence, setRetrySequence] = useState(0);
  const currentLocalDate = useCurrentLocalDate();
  const validatedRange = useMemo(() => {
    try {
      return { value: validateUsageDateRange(dateRange), error: null };
    } catch (error) {
      return {
        value: null,
        error: error instanceof Error ? error.message : "日期范围不合法"
      };
    }
  }, [dateRange]);

  useEffect(() => {
    if (preset === "custom") {
      return;
    }
    setDateRange(presetUsageDateRange(preset));
  }, [currentLocalDate, preset]);

  useEffect(() => {
    if (validatedRange.value === null) {
      setRequestState("failed");
      setErrorMessage(validatedRange.error);
      setReport(null);
      return;
    }
    const controller = new AbortController();
    let isCurrentRequest = true;
    setRequestState("loading");
    setErrorMessage(null);

    void fetchBackgroundAgentUsage(
      baseUrl,
      validatedRange.value,
      controller.signal
    ).then(
      payload => {
        if (!isCurrentRequest) {
          return;
        }
        setReport(payload);
        setRequestState("ready");
      },
      error => {
        if (!isCurrentRequest || (error instanceof DOMException && error.name === "AbortError")) {
          return;
        }
        setReport(null);
        setRequestState("failed");
        setErrorMessage(error instanceof Error ? error.message : "读取后台工作用量失败");
      }
    );

    // 日期变化或离开页面时立即取消旧请求，旧响应没有机会覆盖新筛选结果。
    return () => {
      isCurrentRequest = false;
      controller.abort();
    };
  }, [baseUrl, retrySequence, validatedRange.error, validatedRange.value?.fromDate, validatedRange.value?.toDate]);

  const applyPreset = (nextPreset: Exclude<UsageRangePreset, "custom">): void => {
    setPreset(nextPreset);
    setDateRange(presetUsageDateRange(nextPreset));
  };
  const updateDate = (field: keyof UsageDateRange, value: string): void => {
    setPreset("custom");
    setDateRange(current => ({ ...current, [field]: value }));
  };

  return (
    <section className="space-y-6">
      <PageHeader
        title="后台工作用量统计"
        description="Token 是权威口径；SDK 参考成本只用于观察，不代表真实账单或限额。"
      />

      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 pt-6">
          <div aria-label="快捷时间范围" className="flex flex-wrap gap-2">
            <PresetButton active={preset === "today"} onClick={() => applyPreset("today")}>今天</PresetButton>
            <PresetButton active={preset === "last7Days"} onClick={() => applyPreset("last7Days")}>近 7 天</PresetButton>
            <PresetButton active={preset === "last30Days"} onClick={() => applyPreset("last30Days")}>近 30 天</PresetButton>
          </div>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            开始日期
            <Input
              aria-label="开始日期"
              className="w-40"
              onChange={event => updateDate("fromDate", event.target.value)}
              type="date"
              value={dateRange.fromDate}
            />
          </label>
          <label className="grid gap-1 text-xs font-medium text-muted-foreground">
            结束日期
            <Input
              aria-label="结束日期"
              className="w-40"
              onChange={event => updateDate("toDate", event.target.value)}
              type="date"
              value={dateRange.toDate}
            />
          </label>
        </CardContent>
      </Card>

      {requestState === "loading" ? (
        <EmptyState>正在读取后台工作用量，请稍候。</EmptyState>
      ) : null}
      {requestState === "failed" ? (
        <Card className="border-destructive/30">
          <CardContent className="flex flex-wrap items-center justify-between gap-3 pt-6">
            <p className="text-sm text-destructive">{errorMessage ?? "读取后台工作用量失败"}</p>
            <Button onClick={() => setRetrySequence(value => value + 1)} size="sm" variant="outline">
              重试
            </Button>
          </CardContent>
        </Card>
      ) : null}
      {requestState === "ready" && report !== null ? (
        <>
          <UsageSummaryCards summary={report.summary} />
          {report.summary.task_count === 0 ? (
            <EmptyState>所选时间范围内还没有后台工作用量。</EmptyState>
          ) : (
            <>
              <UsageByModelTable rows={report.by_model} />
              <UsageDailyTrend
                fromDate={report.from_date}
                rows={report.by_day}
                toDate={report.to_date}
              />
            </>
          )}
        </>
      ) : null}
    </section>
  );
}

type PresetButtonProps = {
  active: boolean;
  onClick(): void;
  children: string;
};

/** 渲染一个具有明确选中态的时间预设按钮。 */
function PresetButton({ active, onClick, children }: PresetButtonProps): JSX.Element {
  return (
    <Button
      aria-pressed={active}
      onClick={onClick}
      size="sm"
      type="button"
      variant={active ? "secondary" : "outline"}
    >
      {children}
    </Button>
  );
}
