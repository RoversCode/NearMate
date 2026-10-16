import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import { App } from "./App";

// 此套用例覆盖普通控制台的领域行为，运行时边界由 RuntimeInitializationGate 测试单独验证。
vi.mock("./features/runtime-initialization/RuntimeInitializationGate", () => ({
  RuntimeInitializationGate: ({ children }: { children: import("react").ReactNode }) => children
}));
import {
  createStreamVoxRuntimePayload,
  createConsoleFetchMock,
  resetConsoleAppTestEnvironment
} from "./test/consoleAppTestHarness";

afterEach(resetConsoleAppTestEnvironment);

describe("console app character cards", () => {
  it("包含 expected_tool 的真实双角色夹具可以正常加载", async () => {
    vi.stubGlobal("fetch", createConsoleFetchMock({ includeYaeMikoCard: true }));

    render(<App />);

    fireEvent.click(await screen.findByText("角色卡"));

    expect(await screen.findByRole("button", { name: /火花/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /八重神子/ })).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    });
    fireEvent.click(screen.getByRole("button", { name: "模型示例" }));
    const expectedToolSelect = screen.getByLabelText("预期工具 1") as HTMLSelectElement;
    expect(Array.from(expectedToolSelect.options).map((option) => option.value)).toEqual([
      "",
      "lookup_current_fact",
      "analyze_deeply",
      "start_background_work"
    ]);
    expect(expectedToolSelect.value).toBe("lookup_current_fact");
    expect((screen.getByLabelText("预期工具 2") as HTMLSelectElement).value).toBe("analyze_deeply");
  });

  it("角色卡契约读取失败时展示错误而不是空列表", async () => {
    const normalFetch = createConsoleFetchMock();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!input.toString().endsWith("/api/character-cards")) {
        return normalFetch(input, init);
      }

      return new Response(
        JSON.stringify({
          cards: [
            {
              voiceprint: {
                streamvox_examples: {
                  generic: {
                    items: [{ title: "旧示例", user: "你好", char: "你好呀", route: null, brief: null }]
                  },
                  models: {}
                }
              }
            }
          ],
          active_card_id: "card-1",
          shared_world_canon: ""
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    fireEvent.click(await screen.findByText("角色卡"));

    expect(await screen.findByText("角色卡加载失败")).toBeTruthy();
    expect(screen.getByText("读取角色卡失败：响应格式不符合控制台契约")).toBeTruthy();
    expect(screen.queryByText("没有可编辑的角色卡。")).toBeNull();
  });

  it("上传角色卡参考音频只更新草稿，不立即保存角色卡", async () => {
      const fetchMock = createConsoleFetchMock();
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("角色卡"));
      fireEvent.click(await screen.findByRole("button", { name: /火花/ }));
      fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));

      const audioFile = new File(["audio"], "reference.wav", { type: "audio/wav" });
      fireEvent.change(screen.getByLabelText("上传参考音频"), {
        target: {
          files: [audioFile]
        }
      });

      await waitFor(() => {
        expect(screen.getByText("参考音频已上传，保存当前卡后生效")).toBeTruthy();
      });
      expect(
        fetchMock.mock.calls.some(
          ([calledInput, init]) => calledInput.toString().endsWith("/api/character-cards/card-1") && init?.method === "PUT"
        )
      ).toBe(false);
    });

  it("应用非激活角色语音后仍停留在正在编辑的角色卡", async () => {
      const fetchMock = createConsoleFetchMock({ includeYaeMikoCard: true });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("角色卡"));
      fireEvent.click(await screen.findByRole("button", { name: /八重神子/ }));
      expect(await screen.findByRole("heading", { name: "八重神子" })).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { name: "应用此角色语音" }));

      await waitFor(() => {
        expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/character-cards/yae-miko/voice/apply"))).toBe(
          true
        );
      });
      expect(screen.getByRole("heading", { name: "八重神子" })).toBeTruthy();
      expect(screen.queryByRole("heading", { name: "火花" })).toBeNull();
    });

  it("激活语音未匹配角色卡时会询问是否同步应用 StreamVox 语音", async () => {
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValueOnce(true);
      const fetchMock = createConsoleFetchMock({
        streamVoxRuntime: {
          ...createStreamVoxRuntimePayload(),
          role_name: "demo_role",
          roles: ["demo_role", "huohua"]
        }
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("角色卡"));
      fireEvent.click(await screen.findByRole("button", { name: /火花/ }));
      fireEvent.click(await screen.findByRole("button", { name: "激活卡" }));

      await waitFor(() => {
        expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("是否同时切换 StreamVox"));
        expect(fetchMock.mock.calls.some(([calledInput]) => calledInput.toString().endsWith("/api/character-cards/card-1/voice/apply"))).toBe(true);
      });

      confirmSpy.mockRestore();
    });

  it("StreamVox 语音状态暂时不可读时仍允许激活角色卡", async () => {
      const defaultFetch = createConsoleFetchMock();
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (input.toString().endsWith("/api/character-cards/card-1/voice-status")) {
          return new Response(JSON.stringify({ detail: "StreamVox 暂不可用" }), {
            status: 503,
            headers: { "Content-Type": "application/json" }
          });
        }
        return defaultFetch(input, init);
      });
      vi.stubGlobal("fetch", fetchMock);

      render(<App />);

      fireEvent.click(await screen.findByText("角色卡"));
      fireEvent.click(await screen.findByRole("button", { name: /火花/ }));
      fireEvent.click(await screen.findByRole("button", { name: "激活卡" }));

      await waitFor(() => {
        expect(fetchMock.mock.calls.some(([calledInput, requestInit]) =>
          calledInput.toString().endsWith("/api/character-cards/card-1/activate") && requestInit?.method === "POST"
        )).toBe(true);
      });
      expect(await screen.findByText(/角色卡语音状态暂时不可用/)).toBeTruthy();
      expect(screen.queryByText(/角色卡激活错误/)).toBeNull();
    });
});
