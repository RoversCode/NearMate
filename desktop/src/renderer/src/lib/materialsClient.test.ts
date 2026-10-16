import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchCurrentMaterialContext } from "./materialsClient";

describe("materialsClient", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("首次加载校验 revisioned 材料快照", async () => {
    const payload = {
      revision: 3,
      turn_material_context: { staged_materials: [], active: null },
      recent_materials: []
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    })));

    await expect(fetchCurrentMaterialContext("http://127.0.0.1:8765/")).resolves.toEqual(payload);
  });
});
