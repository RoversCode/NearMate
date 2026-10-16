import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSkillDetail,
  createSkillLibraryResponse,
  createSkillUpdateResponse
} from "../../test/skillFixtures";
import { fetchSkillDetail, fetchSkillLibrary, updateSkillEnabled } from "./skills";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("skill library service", () => {
  it("读取并校验技能库列表", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(createSkillLibraryResponse()));
    vi.stubGlobal("fetch", fetchMock);

    const response = await fetchSkillLibrary("http://127.0.0.1:8765/");

    expect(response.summary).toEqual({ total: 2, enabled: 1, ready: 1, usable: 1 });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/console/skills"
    );
  });

  it("拒绝和 Skill 列表不一致的汇总数字", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(
      createSkillLibraryResponse({
        summary: { total: 99, enabled: 1, ready: 1, usable: 1 }
      })
    )));

    await expect(fetchSkillLibrary("http://127.0.0.1:8765")).rejects.toThrow(
      "读取技能库失败：响应格式不符合契约"
    );
  });

  it("读取详情时拒绝响应中的错误 Skill 身份", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(
      createSkillDetail({ id: "another-skill" })
    )));

    await expect(
      fetchSkillDetail("http://127.0.0.1:8765", "ffmpeg-convert")
    ).rejects.toThrow("读取 Skill 详情失败：响应格式不符合契约");
  });

  it("拒绝把 Windows UNC 或根相对路径当成 Skill 内部文件", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(
      createSkillDetail({ files: ["\\\\server\\share\\secret.txt"] })
    )));

    await expect(
      fetchSkillDetail("http://127.0.0.1:8765", "ffmpeg-convert")
    ).rejects.toThrow("读取 Skill 详情失败：响应格式不符合契约");
  });

  it("提交版本化开关并读取结构化冲突错误", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(createSkillUpdateResponse(false), 202))
      .mockResolvedValueOnce(jsonResponse({
        detail: {
          code: "skill_revision_conflict",
          message: "Skill 设置版本已变化"
        }
      }, 409));
    vi.stubGlobal("fetch", fetchMock);

    const response = await updateSkillEnabled(
      "http://127.0.0.1:8765",
      "ffmpeg-convert",
      false,
      10
    );
    expect(response.revision).toBe(11);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      method: "PATCH",
      body: JSON.stringify({ enabled: false, expected_revision: 10 })
    });

    await expect(
      updateSkillEnabled("http://127.0.0.1:8765", "ffmpeg-convert", true, 11)
    ).rejects.toThrow("开启 Skill 失败：Skill 设置版本已变化");
  });
});

/** 构造带 JSON Content-Type 的测试响应。 */
function jsonResponse(payload: object, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
