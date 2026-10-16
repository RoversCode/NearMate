import { describe, expect, it, vi } from "vitest";
import {
  ACTIVE_CHARACTER_NAME_FALLBACK,
  fetchActiveCharacterName,
  readActiveCharacterName
} from "./characterName";

describe("characterName", () => {
  it("读取角色卡 meta.name 并清理首尾空白", () => {
    expect(readActiveCharacterName({ card: { meta: { name: "  星澜  " } } })).toBe("星澜");
  });

  it("不把缺失或空名称当成有效角色名", () => {
    expect(readActiveCharacterName({ card: null })).toBeNull();
    expect(readActiveCharacterName({ card: { meta: { name: "   " } } })).toBeNull();
    expect(ACTIVE_CHARACTER_NAME_FALLBACK).toBe("当前角色");
  });

  it("从激活角色卡接口读取当前名称", async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue({
      ok: true,
      json: async () => ({ card: { meta: { name: "星澜" } } })
    } as Response);

    await expect(fetchActiveCharacterName("http://127.0.0.1:8765/", request)).resolves.toBe("星澜");
    expect(request).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/character-cards/active"
    );
  });

  it("HTTP、网络或协议失败时返回 null，让调用方保留既有名称", async () => {
    const failedResponse = vi.fn<typeof fetch>().mockResolvedValue({ ok: false } as Response);
    await expect(fetchActiveCharacterName("http://localhost", failedResponse)).resolves.toBeNull();

    const networkFailure = vi.fn<typeof fetch>().mockRejectedValue(new Error("offline"));
    await expect(fetchActiveCharacterName("http://localhost", networkFailure)).resolves.toBeNull();

    const invalidPayload = vi.fn<typeof fetch>().mockResolvedValue({
      ok: true,
      json: async () => ({ card: { meta: { name: 7 } } })
    } as Response);
    await expect(fetchActiveCharacterName("http://localhost", invalidPayload)).resolves.toBeNull();
  });
});
