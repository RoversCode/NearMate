// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createReadySkill,
  createSkillLibraryResponse,
  createSkillUpdateResponse
} from "../../test/skillFixtures";
import { useSkillLibrary, type SkillLibraryController } from "./useSkillLibrary";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useSkillLibrary", () => {
  it("同一次 React 提交前的连续动作也只发送一个版本化 PATCH", async () => {
    const firstSkill = createReadySkill({ id: "first-skill", name: "第一项" });
    const secondSkill = createReadySkill({ id: "second-skill", name: "第二项" });
    let resolveUpdate: (response: Response) => void = () => undefined;
    const pendingUpdate = new Promise<Response>(resolve => {
      resolveUpdate = resolve;
    });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return pendingUpdate;
      }
      return jsonResponse(createSkillLibraryResponse({ skills: [firstSkill, secondSkill] }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SkillLibraryProbe />);
    await screen.findByText("ready");
    fireEvent.click(screen.getByRole("button", { name: "连续提交" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0].toString()).toContain("/first-skill");

    await act(async () => {
      resolveUpdate(jsonResponse(createSkillUpdateResponse(false, {
        skill: createReadySkill({ id: "first-skill", name: "第一项", enabled: false, usable: false })
      }), 202));
      await pendingUpdate;
    });
    await screen.findByText("idle");
  });

  it("开关 PATCH 已成功但设置快照刷新失败时仍保留成功状态", async () => {
    const skill = createReadySkill({ id: "first-skill", name: "第一项", enabled: true, usable: true });
    const refreshSettingsSnapshot = vi.fn().mockRejectedValue(new Error("设置快照暂时不可用"));
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return jsonResponse(createSkillUpdateResponse(false, {
          skill: createReadySkill({ id: "first-skill", name: "第一项", enabled: false, usable: false })
        }), 202);
      }
      return jsonResponse(createSkillLibraryResponse({ skills: [skill] }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SkillLibraryProbe onSettingsSnapshotRefresh={refreshSettingsSnapshot} />);
    await screen.findByText("ready");
    fireEvent.click(screen.getByRole("button", { name: "关闭第一项" }));

    await waitFor(() => expect(refreshSettingsSnapshot).toHaveBeenCalledOnce());
    expect(await screen.findByText(/已保存，但页面状态刷新失败：设置快照暂时不可用/)).toBeTruthy();
    expect(screen.getByText("idle")).toBeTruthy();
  });

  it("开关 PATCH 失败后也刷新普通设置版本，避免下一次保存继续使用旧 revision", async () => {
    const refreshSettingsSnapshot = vi.fn(async () => undefined);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(createSkillLibraryResponse()))
      .mockResolvedValueOnce(jsonResponse({ detail: "Skill 设置版本已变化" }, 409))
      .mockResolvedValueOnce(jsonResponse(createSkillLibraryResponse({ revision: 12 })));
    vi.stubGlobal("fetch", fetchMock);

    render(<SkillLibraryProbe onSettingsSnapshotRefresh={refreshSettingsSnapshot} />);
    await screen.findByText("ready");
    fireEvent.click(screen.getByRole("button", { name: "关闭FFmpeg 格式转换" }));

    await waitFor(() => expect(refreshSettingsSnapshot).toHaveBeenCalledOnce());
    expect(await screen.findByText("关闭 Skill 失败：Skill 设置版本已变化")).toBeTruthy();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  });
});

/** 暴露 Hook 状态，并在同一事件中故意连续请求两个不同 Skill。 */
function SkillLibraryProbe({ onSettingsSnapshotRefresh }: { onSettingsSnapshotRefresh?: () => Promise<unknown> } = {}): JSX.Element {
  const controller: SkillLibraryController = useSkillLibrary("http://127.0.0.1:8765", onSettingsSnapshotRefresh);
  return (
    <div>
      <output>{controller.requestState}</output>
      <output>{controller.updatingSkillId ?? "idle"}</output>
      <output>{controller.updateErrorMessage ?? "no-error"}</output>
      <output>{controller.updateWarningMessage ?? "no-warning"}</output>
      {controller.snapshot?.skills.map(skill => (
        <button key={skill.id} onClick={() => void controller.setSkillEnabled(skill.id, !skill.enabled)} type="button">
          {skill.enabled ? `关闭${skill.name}` : `开启${skill.name}`}
        </button>
      ))}
      <button
        onClick={() => {
          void controller.setSkillEnabled("first-skill", false);
          void controller.setSkillEnabled("second-skill", false);
        }}
        type="button"
      >
        连续提交
      </button>
    </div>
  );
}

/** 构造带 JSON Content-Type 的测试响应。 */
function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
