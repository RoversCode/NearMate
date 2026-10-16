import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ToolWaitingPolicyPanel } from "./ToolWaitingPolicyPanel";

function createPayload() {
  return {
    families: [
      {
        family_id: "weather",
        display_name: "天气",
        latency_class: "external_api",
        default_waiting_enabled: true,
        default_waiting_phrases: ["好，我查一下天气。"],
        builtin_policy: { enabled: true, phrases: ["好，我查一下天气。"] },
        user_policy: null,
        effective_policy: { enabled: true, phrases: ["好，我查一下天气。"], source: "builtin" },
        tools: [
          {
            name: "weather_overview",
            display_name: "实时天气",
            family_id: "weather",
            family_display_name: "天气",
            latency_class: "external_api",
            user_override: null,
            effective_policy: { enabled: true, phrases: ["好，我查一下天气。"], source: "builtin" }
          }
        ]
      },
      {
        family_id: "datetime",
        display_name: "日期时间",
        latency_class: "instant",
        default_waiting_enabled: false,
        default_waiting_phrases: [],
        builtin_policy: { enabled: false, phrases: [] },
        user_policy: null,
        effective_policy: { enabled: false, phrases: [], source: "disabled" },
        tools: []
      }
    ],
    user_config: { families: {}, tools: {} }
  };
}

function createInstantOnlyPayload() {
  return {
    families: [
      {
        family_id: "datetime",
        display_name: "日期时间",
        latency_class: "instant",
        default_waiting_enabled: false,
        default_waiting_phrases: [],
        builtin_policy: { enabled: false, phrases: [] },
        user_policy: null,
        effective_policy: { enabled: false, phrases: [], source: "disabled" },
        tools: []
      }
    ],
    user_config: { families: {}, tools: {} }
  };
}

describe("ToolWaitingPolicyPanel", () => {
  it("默认显示紧凑分类行，展开后才显示等待句编辑器", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(createPayload()))));
    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    const editButton = await screen.findByRole("button", { name: "编辑天气等待反馈" });
    expect(editButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(editButton);
    expect(editButton.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByLabelText("天气等待句")).toBeTruthy();
    expect((screen.getByRole("button", { name: "保存天气策略" }) as HTMLButtonElement).disabled).toBe(true);
    vi.unstubAllGlobals();
  });

  it("切换开关不提交也不清除尚未保存的文案", async () => {
    const payload = createPayload();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") payload.families[0].effective_policy.enabled = false;
      return new Response(JSON.stringify(payload));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑天气等待反馈" }));
    fireEvent.change(screen.getByLabelText("天气等待句"), { target: { value: "还没有保存的文案。" } });
    fireEvent.click(screen.getByRole("switch", { name: "关闭天气等待句播报" }));
    await screen.findByText("天气等待反馈已关闭。");

    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      enabled: false, phrases: ["好，我查一下天气。"]
    });
    expect((screen.getByLabelText("天气等待句") as HTMLTextAreaElement).value).toBe("还没有保存的文案。");
    expect(screen.getByRole("switch", { name: "开启天气等待句播报" }).getAttribute("aria-checked")).toBe("false");
    vi.unstubAllGlobals();
  });

  it("保存失败保留文案，并提供可再次提交的按钮", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") throw new Error("保存连接中断");
      return new Response(JSON.stringify(createPayload()));
    }));
    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑天气等待反馈" }));
    fireEvent.change(screen.getByLabelText("天气等待句"), { target: { value: "需要重试的文案。" } });
    fireEvent.click(screen.getByRole("button", { name: "保存天气策略" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("保存连接中断")).toBeTruthy();
    expect((screen.getByLabelText("天气等待句") as HTMLTextAreaElement).value).toBe("需要重试的文案。");
    expect((screen.getByRole("button", { name: "保存天气策略" }) as HTMLButtonElement).disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("首次读取失败显示错误并允许重试", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error("本机服务暂时不可用"))
      .mockResolvedValueOnce(new Response(JSON.stringify(createPayload()), {
        headers: { "Content-Type": "application/json" }
      }));
    vi.stubGlobal("fetch", fetchMock);

    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    expect(await screen.findByText(/本机服务暂时不可用/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("天气")).toBeTruthy();
    expect(screen.queryByText(/本机服务暂时不可用/)).toBeNull();
    vi.unstubAllGlobals();
  });

  it("保存关闭中的工具族文案不会开启播报", async () => {
    const payload = createPayload();
    payload.families[0].effective_policy.enabled = false;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑天气等待反馈" }));
    fireEvent.change(await screen.findByLabelText("天气等待句"), { target: { value: "新的等待句。" } });
    fireEvent.click(screen.getByText("保存天气策略"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      enabled: false, phrases: ["新的等待句。"]
    });
    vi.unstubAllGlobals();
  });

  it("保存一个工具族后保留其他族和工具未保存的编辑", async () => {
    const payload = createPayload();
    payload.families.push({ ...payload.families[0], family_id: "search", display_name: "搜索", tools: [] });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑搜索等待反馈" }));
    fireEvent.change(await screen.findByLabelText("搜索等待句"), { target: { value: "搜索草稿。" } });
    fireEvent.click(screen.getByRole("button", { name: "编辑天气等待反馈" }));
    fireEvent.click(screen.getByText("单独设置工具（1）"));
    fireEvent.click(screen.getByText("自定义实时天气"));
    fireEvent.change(screen.getByLabelText("实时天气等待句"), { target: { value: "单工具草稿。" } });
    fireEvent.change(screen.getByLabelText("天气等待句"), { target: { value: "新的天气等待句。" } });
    fireEvent.click(screen.getByText("保存天气策略"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((screen.getByLabelText("搜索等待句") as HTMLTextAreaElement).value).toBe("搜索草稿。"));
    expect((screen.getByLabelText("实时天气等待句") as HTMLTextAreaElement).value).toBe("单工具草稿。");
    expect((screen.getByLabelText("实时天气等待句") as HTMLTextAreaElement).disabled).toBe(false);
    vi.unstubAllGlobals();
  });

  it("支持编辑保存 family 默认等待句", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return new Response(JSON.stringify(createPayload()), {
          status: 202,
          headers: { "Content-Type": "application/json" }
        });
      }
      return new Response(JSON.stringify(createPayload()), {
        headers: { "Content-Type": "application/json" }
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑天气等待反馈" }));
    expect(screen.queryByText("日期时间")).toBeNull();
    fireEvent.change(screen.getByLabelText("天气等待句"), { target: { value: "我看一下天气。\n稍等，我查天气。" } });
    fireEvent.click(screen.getByText("保存天气策略"));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8765/api/tool-waiting-policies/families/weather",
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ enabled: true, phrases: ["我看一下天气。", "稍等，我查天气。"] })
        })
      )
    );

    vi.unstubAllGlobals();
  });

  it("没有可配置等待句工具时展示空态", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify(createInstantOnlyPayload()), {
        headers: { "Content-Type": "application/json" }
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    expect(await screen.findByText("暂无需要等待反馈的工具。")).toBeTruthy();
    expect(screen.queryByText("日期时间")).toBeNull();

    vi.unstubAllGlobals();
  });

  it("支持保存和清除单工具 override", async () => {
    const payload = createPayload();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      if (_init?.method === "PUT") {
        const submitted = JSON.parse(String(_init.body));
        const tool = payload.families[0].tools[0];
        if (!submitted.inherit_family) {
          Object.assign(tool, {
            user_override: { enabled: submitted.enabled, phrases: submitted.phrases },
            effective_policy: { enabled: submitted.enabled, phrases: submitted.phrases, source: "tool_override" }
          });
        }
      }
      return new Response(JSON.stringify(payload), {
        status: 202,
        headers: { "Content-Type": "application/json" }
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<ToolWaitingPolicyPanel baseUrl="http://127.0.0.1:8765" />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑天气等待反馈" }));
    fireEvent.click(screen.getByText("单独设置工具（1）"));
    fireEvent.click(screen.getByText("自定义实时天气"));
    fireEvent.change(screen.getByLabelText("实时天气等待句"), { target: { value: "我单独查实时天气。" } });
    fireEvent.click(screen.getByText("保存实时天气策略"));

    await screen.findByText("实时天气等待句已保存。");
    fireEvent.click(screen.getByText("继承天气"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8765/api/tool-waiting-policies/tools/weather_overview");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      inherit_family: false,
      enabled: true,
      phrases: ["我单独查实时天气。"]
    });
    expect(JSON.parse(String(fetchMock.mock.calls[2][1]?.body))).toEqual({
      inherit_family: true
    });

    vi.unstubAllGlobals();
  });
});
