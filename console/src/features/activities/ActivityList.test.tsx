// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ActivitySnapshot } from "../../api/contracts/activities";
import { ActivityList } from "./ActivityList";

describe("ActivityList", () => {
  it("失败任务在目标下方直接显示公开摘要", () => {
    const snapshot: ActivitySnapshot = {
      revision: 3,
      runtime_id: "00000000-0000-4000-8000-0000000000aa",
      active: [],
      recent: [
        {
          activity_id: "01890f3e-7cc2-7d00-8abc-1234567890ab",
          kind: "task_result",
          type_label: "任务结果",
          status: "failed",
          goal_summary: "整理项目材料",
          failure_summary: "模型服务暂时不可用。",
          stage_code: "processing",
          stage_label: "分析与处理",
          started_at: null,
          updated_at: "2026-07-18T10:01:00+08:00",
          finished_at: "2026-07-18T10:01:00+08:00",
          can_cancel: false
        }
      ]
    };

    render(
      <ActivityList
        snapshot={snapshot}
        cancellingIds={new Set()}
        now={new Date("2026-07-18T10:01:00+08:00")}
        onCancel={vi.fn()}
        onOpenDetail={vi.fn()}
      />
    );

    expect(screen.getByText("整理项目材料")).toBeTruthy();
    expect(screen.getByText("模型服务暂时不可用。")).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
  });

  it("同时渲染两项活动，并按各自 Activity 编号独立取消", () => {
    const onCancel = vi.fn();
    const active = [
      {
        activity_id: "01890f3e-7cc2-7d00-8abc-1234567890ab",
        kind: "background_agent",
        type_label: "后台工作",
        status: "running" as const,
        goal_summary: "整理会议录音",
        failure_summary: null,
        stage_code: "processing" as const,
        stage_label: "分析与处理",
        started_at: "2026-07-18T10:00:00+08:00",
        updated_at: "2026-07-18T10:00:01+08:00",
        finished_at: null,
        can_cancel: true
      },
      {
        activity_id: "01890f3e-7cc2-7d00-8abc-1234567890ac",
        kind: "background_agent",
        type_label: "后台工作",
        status: "waiting_user" as const,
        goal_summary: "恢复报表转换",
        failure_summary: null,
        stage_code: "processing" as const,
        stage_label: "等待补充",
        started_at: "2026-07-18T10:00:10+08:00",
        updated_at: "2026-07-18T10:00:11+08:00",
        finished_at: null,
        can_cancel: true
      }
    ];

    render(
      <ActivityList
        snapshot={{ revision: 4, runtime_id: "00000000-0000-4000-8000-0000000000aa", active, recent: [] }}
        cancellingIds={new Set()}
        now={new Date("2026-07-18T10:01:00+08:00")}
        onCancel={onCancel}
        onOpenDetail={vi.fn()}
      />
    );

    for (const button of screen.getAllByRole("button", { name: "取消" })) {
      fireEvent.click(button);
    }
    expect(onCancel).toHaveBeenNthCalledWith(1, active[0].activity_id);
    expect(onCancel).toHaveBeenNthCalledWith(2, active[1].activity_id);
  });
});
