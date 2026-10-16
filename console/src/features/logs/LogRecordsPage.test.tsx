// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LogRecordsPage } from "./LogRecordsPage";

describe("LogRecordsPage", () => {
  it("明确标注本次运行并格式化结构化 payload", () => {
    render(<LogRecordsPage rows={[{
      sequence: 9,
      event_type: "activities.snapshot_changed",
      payload: { revision: 4, active: [] },
      recorded_at: "2026-07-18T10:00:00+08:00"
    }]} />);

    expect(screen.getByRole("heading", { name: "本次运行日志" })).toBeTruthy();
    expect(screen.getByText("9")).toBeTruthy();
    expect(screen.getByText(/"revision": 4/)).toBeTruthy();
    expect(screen.queryByText("[object Object]")).toBeNull();
  });

  it("空日志说明会随应用重启清空", () => {
    render(<LogRecordsPage rows={[]} />);
    expect(screen.getByText("本次运行暂无日志。")).toBeTruthy();
    expect(screen.getByText(/应用重启后自动清空/)).toBeTruthy();
  });

  it("搜索载荷并可清空筛选，日志详情默认收起", () => {
    render(<LogRecordsPage rows={[
      { sequence: 1, event_type: "audio.started", recorded_at: "2026-09-29", payload: { request: "voice-123" } },
      { sequence: 2, event_type: "settings.changed", recorded_at: "2026-09-29", payload: { revision: 2 } }
    ]} />);
    expect(document.querySelector("details")?.open).toBe(false);
    fireEvent.change(screen.getByLabelText("搜索运行日志"), { target: { value: "voice-123" } });
    expect(screen.getByText("audio.started")).toBeTruthy();
    expect(screen.queryByText("settings.changed")).toBeNull();
    fireEvent.change(screen.getByLabelText("搜索运行日志"), { target: { value: "missing" } });
    fireEvent.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(screen.getByText("settings.changed")).toBeTruthy();
  });
});
