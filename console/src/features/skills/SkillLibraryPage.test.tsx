// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSkillDetail,
  createSkillLibraryResponse,
  createReadySkill,
  createUnavailableSkill,
  createSkillUpdateResponse
} from "../../test/skillFixtures";
import { SkillLibraryPage } from "./SkillLibraryPage";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("skill library page", () => {
  it("展示汇总并支持搜索和分类筛选", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(createSkillLibraryResponse())));
    render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    expect(await screen.findByText("FFmpeg 格式转换")).toBeTruthy();
    expect(screen.getByText("图片文字提取")).toBeTruthy();
    expect(getSummaryValue("全部 Skill")).toBe("2");
    expect(getSummaryValue("已开启")).toBe("1");
    expect(getSummaryValue("下一任务可用")).toBe("1");
    expect(getSummaryValue("依赖缺失")).toBe("1");

    fireEvent.change(screen.getByPlaceholderText("搜索名称、能力、分类或 ID"), {
      target: { value: "目标格式或编码要求" }
    });
    expect(screen.getByText("FFmpeg 格式转换")).toBeTruthy();
    expect(screen.queryByText("图片文字提取")).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("搜索名称、能力、分类或 ID"), {
      target: { value: "图片" }
    });
    expect(screen.queryByText("FFmpeg 格式转换")).toBeNull();
    expect(screen.getByText("图片文字提取")).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText("搜索名称、能力、分类或 ID"), {
      target: { value: "" }
    });
    fireEvent.change(screen.getByLabelText("Skill 分类"), {
      target: { value: "媒体处理" }
    });
    expect(screen.getByText("FFmpeg 格式转换")).toBeTruthy();
    expect(screen.queryByText("图片文字提取")).toBeNull();
  });

  it("按需读取详情并把 SKILL.md 当作纯文本展示", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      return url.endsWith("/ffmpeg-convert")
        ? jsonResponse(createSkillDetail())
        : jsonResponse(createSkillLibraryResponse());
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    fireEvent.click(await screen.findByRole("button", { name: "查看 FFmpeg 格式转换 详情" }));

    expect(await screen.findByRole("dialog", { name: "FFmpeg 格式转换" })).toBeTruthy();
    expect(await screen.findByText("scripts/convert_media.py")).toBeTruthy();
    expect(screen.getByText("运行依赖")).toBeTruthy();
    expect(screen.getByText("已检测到版本 7.1.0")).toBeTruthy();
    fireEvent.click(screen.getByText("查看 SKILL.md 原文"));
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeTruthy();
    expect(view.container.querySelector("img")).toBeNull();
    expect(fetchMock.mock.calls.some(([input]) => input.toString().endsWith("/api/console/skills/ffmpeg-convert"))).toBe(true);
  });

  it("携带列表版本关闭 Skill 并立即更新当前行", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "PATCH"
        ? jsonResponse(createSkillUpdateResponse(false), 202)
        : jsonResponse(createSkillLibraryResponse())
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    const switchElement = await screen.findByRole("switch", { name: "关闭 FFmpeg 格式转换" });
    fireEvent.click(switchElement);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ enabled: false, expected_revision: 10 })
    });
    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "开启 FFmpeg 格式转换" }).getAttribute("data-state")).toBe("unchecked");
    });
    expect(getSummaryValue("已开启")).toBe("0");
    expect(getSummaryValue("下一任务可用")).toBe("0");
  });

  it("Skill 保存成功后请求同步普通设置版本", async () => {
    const refreshSettingsSnapshot = vi.fn(async () => undefined);
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "PATCH"
        ? jsonResponse(createSkillUpdateResponse(false), 202)
        : jsonResponse(createSkillLibraryResponse())
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <SkillLibraryPage
        baseUrl="http://127.0.0.1:8765"
        onSettingsSnapshotRefresh={refreshSettingsSnapshot}
      />
    );

    fireEvent.click(await screen.findByRole("switch", { name: "关闭 FFmpeg 格式转换" }));

    await waitFor(() => expect(refreshSettingsSnapshot).toHaveBeenCalledOnce());
  });

  it("设置快照刷新失败时把已保存提醒展示为状态提示，而不是保存失败", async () => {
    const refreshSettingsSnapshot = vi.fn(async () => {
      throw new Error("设置快照暂时不可用");
    });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "PATCH"
        ? jsonResponse(createSkillUpdateResponse(false), 202)
        : jsonResponse(createSkillLibraryResponse())
    );
    vi.stubGlobal("fetch", fetchMock);
    render(
      <SkillLibraryPage
        baseUrl="http://127.0.0.1:8765"
        onSettingsSnapshotRefresh={refreshSettingsSnapshot}
      />
    );

    fireEvent.click(await screen.findByRole("switch", { name: "关闭 FFmpeg 格式转换" }));

    const warning = await screen.findByRole("status");
    expect(warning.textContent).toContain("已保存，但页面状态刷新失败：设置快照暂时不可用");
    expect(screen.queryByText("保存失败")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("依赖缺失时仍保存开启偏好并明确当前不加载", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "PATCH"
        ? jsonResponse({
            skill: createUnavailableSkill({ enabled: true }),
            revision: 11
          }, 202)
        : jsonResponse(createSkillLibraryResponse())
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    const unavailableSwitch = await screen.findByRole("switch", { name: "开启 图片文字提取" });
    expect((unavailableSwitch as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(unavailableSwitch);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ enabled: true, expected_revision: 10 })
    });
    expect(screen.getByText("偏好已开启，当前不加载")).toBeTruthy();
    expect(screen.getByText("系统找不到命令 tesseract")).toBeTruthy();
    const updatedSwitch = screen.getByRole("switch", { name: "关闭 图片文字提取" });
    const statusDescriptionId = updatedSwitch.getAttribute("aria-describedby");
    expect(statusDescriptionId).not.toBeNull();
    expect(document.getElementById(statusDescriptionId as string)?.textContent).toContain("依赖缺失");
    expect(getSummaryValue("已开启")).toBe("2");
    expect(getSummaryValue("下一任务可用")).toBe("1");
  });

  it("稳定展示并搜索 30 个以上 Skill", async () => {
    const skills = Array.from({ length: 36 }, (_, index) => createReadySkill({
      id: `batch-skill-${index}`,
      name: `批处理能力 ${index}`,
      category: index % 2 === 0 ? "批量处理" : "办公辅助",
      trigger_scenarios: [`第 ${index} 类批量任务`],
      inputs: [`输入类型 ${index}`],
      outputs: [`输出类型 ${index}`]
    }));
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(createSkillLibraryResponse({ skills }))));
    render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    expect(await screen.findAllByRole("switch")).toHaveLength(36);
    fireEvent.change(screen.getByPlaceholderText("搜索名称、能力、分类或 ID"), {
      target: { value: "第 35 类批量任务" }
    });
    expect(screen.getAllByRole("switch")).toHaveLength(1);
    expect(screen.getByText("批处理能力 35")).toBeTruthy();
  });

  it("版本冲突时展示原因并重新读取权威列表", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(createSkillLibraryResponse()))
      .mockResolvedValueOnce(jsonResponse({
        detail: {
          code: "skill_revision_conflict",
          message: "Skill 设置版本已变化"
        }
      }, 409))
      .mockResolvedValueOnce(jsonResponse(createSkillLibraryResponse({ revision: 12 })));
    vi.stubGlobal("fetch", fetchMock);
    render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    fireEvent.click(await screen.findByRole("switch", { name: "关闭 FFmpeg 格式转换" }));

    expect(await screen.findByText("关闭 Skill 失败：Skill 设置版本已变化")).toBeTruthy();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("switch", { name: "关闭 FFmpeg 格式转换" }).getAttribute("data-state")).toBe("checked");
  });

  it("列表失败后可以原地重试", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 503 }))
      .mockResolvedValueOnce(jsonResponse(createSkillLibraryResponse()));
    vi.stubGlobal("fetch", fetchMock);
    render(<SkillLibraryPage baseUrl="http://127.0.0.1:8765" />);

    expect(await screen.findByText("读取技能库失败：HTTP 503")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("FFmpeg 格式转换")).toBeTruthy();
  });
});

/** 构造带 JSON Content-Type 的测试响应。 */
function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

/** 从汇总定义列表中读取指定指标的数字。 */
function getSummaryValue(label: string): string | null {
  const summaryLabel = screen.getAllByText(label).find(element => element.tagName === "DT");
  if (summaryLabel?.parentElement === null || summaryLabel?.parentElement === undefined) {
    throw new Error(`找不到技能库汇总指标：${label}`);
  }
  return within(summaryLabel.parentElement).getByRole("definition").textContent;
}
