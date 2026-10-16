import { describe, expect, it, vi } from "vitest";

import { classifyComposerPaste } from "./chatPasteClient";

describe("classifyComposerPaste", () => {
  it("使用 Backend 唯一 tokenizer 返回粘贴处置", async () => {
    const fetchImplementation = vi.fn(async () => new Response(JSON.stringify({
      disposition: "material",
      token_count: 2001
    }), { status: 200 }));

    await expect(classifyComposerPaste("http://localhost:8765", "长文本", fetchImplementation))
      .resolves.toBe("material");
    expect(fetchImplementation).toHaveBeenCalledWith(
      "http://localhost:8765/api/materials/text-paste/classify",
      expect.objectContaining({ body: JSON.stringify({ text: "长文本" }) })
    );
  });

  it.each([
    ["未知 disposition", new Response(JSON.stringify({ disposition: "unknown" }), { status: 200 })],
    ["缺少 disposition", new Response(JSON.stringify({ token_count: 1 }), { status: 200 })],
    ["坏 JSON", new Response("not-json", { status: 200 })],
    ["非成功响应", new Response("error", { status: 503 })]
  ])("%s 时失败关闭", async (_label, response) => {
    const fetchImplementation = vi.fn(async () => response);

    await expect(
      classifyComposerPaste("http://localhost:8765", "粘贴文本", fetchImplementation)
    ).rejects.toThrow();
  });
});
