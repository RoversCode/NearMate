// @vitest-environment jsdom

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  parseConsoleNavigation,
  serializeConsoleNavigation,
  useConsoleNavigation
} from "./useConsoleNavigation";

function NavigationProbe(): JSX.Element {
  const navigation = useConsoleNavigation();
  return (
    <div>
      <output>{`${navigation.sectionId}:${navigation.taskView}:${navigation.activityId ?? "list"}`}</output>
      <button onClick={() => navigation.navigate("tasks")} type="button">任务</button>
      <button onClick={() => navigation.navigate("tasks", null, "reminders")} type="button">提醒</button>
      <button onClick={() => navigation.navigate("logs")} type="button">日志</button>
    </div>
  );
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("Console URL navigation", () => {
  it("解析任务深链接并归一化非法 section 与 Activity", () => {
    expect(
      parseConsoleNavigation(
        "?section=tasks&activity=01890F3E-7CC2-7D00-8ABC-1234567890AB"
      )
    ).toEqual({
      sectionId: "tasks",
      taskView: "activities",
      activityId: "01890f3e-7cc2-7d00-8abc-1234567890ab"
    });
    expect(parseConsoleNavigation("?section=unknown&activity=bad")).toEqual({
      sectionId: "settings",
      taskView: "activities",
      activityId: null
    });
    expect(parseConsoleNavigation("?section=logs&activity=123e4567-e89b-42d3-a456-426614174000")).toEqual({
      sectionId: "logs",
      taskView: "activities",
      activityId: null
    });
    expect(parseConsoleNavigation("?section=skills")).toEqual({
      sectionId: "skills",
      taskView: "activities",
      activityId: null
    });
  });

  it("序列化时只写入规范白名单参数", () => {
    expect(serializeConsoleNavigation({
      sectionId: "tasks",
      taskView: "activities",
      activityId: null
    })).toBe(
      "?section=tasks&taskView=activities"
    );
    expect(
      serializeConsoleNavigation({
        sectionId: "tasks",
        taskView: "activities",
        activityId: "123e4567-e89b-42d3-a456-426614174000"
      })
    ).toBe(
      "?section=tasks&taskView=activities&activity=123e4567-e89b-42d3-a456-426614174000"
    );
    expect(serializeConsoleNavigation({
      sectionId: "tasks",
      taskView: "reminders",
      activityId: "123e4567-e89b-42d3-a456-426614174000"
    })).toBe("?section=tasks&taskView=reminders");
  });

  it("支持 pushState、浏览器后退与前进恢复", async () => {
    window.history.replaceState(null, "", "/?section=settings");
    render(<NavigationProbe />);

    fireEvent.click(screen.getByRole("button", { name: "任务" }));
    expect(screen.getByText("tasks:activities:list")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "提醒" }));
    expect(screen.getByText("tasks:reminders:list")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "日志" }));
    expect(screen.getByText("logs:activities:list")).toBeTruthy();

    act(() => window.history.back());
    await waitFor(() => expect(screen.getByText("tasks:reminders:list")).toBeTruthy());
    act(() => window.history.forward());
    await waitFor(() => expect(screen.getByText("logs:activities:list")).toBeTruthy());
  });

  it("刷新初始化时会用 replaceState 清理非法参数", async () => {
    window.history.replaceState(null, "", "/?section=tasks&activity=../../bad&url=https://evil.test");
    render(<NavigationProbe />);

    await waitFor(() => expect(window.location.search).toBe("?section=tasks&taskView=activities"));
    expect(screen.getByText("tasks:activities:list")).toBeTruthy();
  });

  it("提醒子视图拒绝 Activity 深链接", () => {
    expect(parseConsoleNavigation(
      "?section=tasks&taskView=reminders&activity=123e4567-e89b-42d3-a456-426614174000"
    )).toEqual({
      sectionId: "tasks",
      taskView: "reminders",
      activityId: null
    });
  });
});
