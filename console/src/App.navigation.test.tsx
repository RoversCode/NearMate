import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { App } from "./App";

// 此套用例覆盖普通控制台的领域行为，运行时边界由 RuntimeInitializationGate 测试单独验证。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import {
  createConsoleSettingsPayload,
  createShortcutStatusResponse,
  createWakeResponseConfigPayload,
  createBootstrapStatusPayload,
  createSupportedLlmModelsPayload,
  createLlmConfigPayload,
  createStreamVoxRuntimePayload,
  createConsoleFetchMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";

afterEach(resetConsoleAppTestEnvironment);

describe("console app navigation", () => {
  it("渲染设置、任务、技能库、用量统计、角色卡、StreamVox 和运行日志七项左侧导航", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByRole("button", { name: "设置" })).toBeTruthy();
      expect(screen.getByText("任务")).toBeTruthy();
      expect(screen.getByText("技能库")).toBeTruthy();
      expect(screen.getByText("用量统计")).toBeTruthy();
      expect(screen.getByText("角色卡")).toBeTruthy();
      expect(screen.getByText("StreamVox")).toBeTruthy();
      expect(screen.getByText("运行日志")).toBeTruthy();
      expect(screen.queryByText("聊天记录")).toBeNull();
      expect(screen.queryByText("任务记录")).toBeNull();
      expect(screen.queryByText("权限记录")).toBeNull();
      expect(screen.queryByText("性能观测")).toBeNull();
      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/console/conversations"))).toBe(false);
      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/console/tasks"))).toBe(false);
      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/console/permissions"))).toBe(false);
      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/console/performance"))).toBe(false);
    });

  it("点击技能库导航时按需读取产品 Skill", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/console/skills"))).toBe(false);
      fireEvent.click(await screen.findByText("技能库"));

      expect(await screen.findByRole("heading", { name: "技能库" })).toBeTruthy();
      expect(await screen.findByText("FFmpeg 格式转换")).toBeTruthy();
      expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/console/skills"))).toBe(true);
    });

  it("点击角色卡导航时会展示角色卡页面并加载角色卡接口", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("角色卡"));

      expect(await screen.findByRole("heading", { name: "角色卡" })).toBeTruthy();
      await waitFor(() => {
        expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/character-cards"))).toBe(
          true
        );
        expect(
          fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/character-cards/active"))
        ).toBe(true);
      });
    });

  it("点击用量统计时按需查询今天并展示空态", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("用量统计"));

      expect(await screen.findByRole("heading", { name: "后台工作用量统计" })).toBeTruthy();
      expect(await screen.findByText("所选时间范围内还没有后台工作用量。")).toBeTruthy();
      expect(
        fetchMock.mock.calls.some(([calledInput]) =>
          calledInput.toString().includes("/api/background-agent/usage?from_date=")
        )
      ).toBe(true);
    });

  it("StreamVox runtime 超时时不会阻断设置页和角色卡页", async () => {
      const fetchMock = createConsoleFetchMock({
        streamVoxRuntimeError: "StreamVox management request timed out: runtime_state"
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(await screen.findByRole("heading", { name: "设置" })).toBeTruthy();
      expect(screen.queryByText("加载失败：读取 StreamVox 状态失败：StreamVox management request timed out: runtime_state")).toBeNull();

      fireEvent.click(screen.getByText("角色卡"));
      expect(await screen.findByRole("heading", { name: "角色卡图库" })).toBeTruthy();

      fireEvent.click(screen.getByText("StreamVox"));
      expect(await screen.findByText("StreamVox 操作失败：读取 StreamVox 状态失败：StreamVox management request timed out: runtime_state")).toBeTruthy();
    });

  it("bootstrap 状态未知时不会提前渲染普通控制台导航", async () => {
      let resolveBootstrapStatus: (response: Response) => void = () => undefined;
      const bootstrapStatusPromise = new Promise<Response>((resolve) => {
        // 延迟 bootstrap 状态返回，专门覆盖首屏门禁尚未决议时的瞬时渲染状态。
        resolveBootstrapStatus = resolve;
      });
      const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
        // 将 RequestInfo 统一转成字符串，兼容组件未来使用 Request 对象或 URL 对象发起请求。
        const url = input.toString();

        if (url.endsWith("/api/bootstrap/status")) {
          return bootstrapStatusPromise;
        }

        if (url.endsWith("/api/llm/config")) {
          return new Response(JSON.stringify(createLlmConfigPayload(true)), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/llm/supported-models")) {
          return new Response(JSON.stringify(createSupportedLlmModelsPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        // 设置页需要真实布尔字段，确保 bootstrap 放行后仍能恢复普通控制台加载。
        if (url.endsWith("/api/settings")) {
          return new Response(JSON.stringify(createConsoleSettingsPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/settings/shortcut-status")) {
          return createShortcutStatusResponse();
        }

        if (url.endsWith("/api/character-cards/card-1/wake-response")) {
          return new Response(JSON.stringify(createWakeResponseConfigPayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        if (url.endsWith("/api/streamvox/runtime")) {
          return new Response(JSON.stringify(createStreamVoxRuntimePayload()), {
            headers: {
              "Content-Type": "application/json"
            }
          });
        }

        // 其他五个历史与观测接口只需成功返回空列表，以便测试聚焦 bootstrap 门禁时序。
        return new Response(JSON.stringify([]), {
          headers: {
            "Content-Type": "application/json"
          }
        });
      });

      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      expect(screen.queryByText("聊天记录")).toBeNull();

      resolveBootstrapStatus(
        new Response(
          JSON.stringify(createBootstrapStatusPayload(true)),
          {
            headers: {
              "Content-Type": "application/json"
            }
          }
        )
      );

      expect(await screen.findByText("StreamVox")).toBeTruthy();
    });
});
