import { describe, expect, it } from "vitest";

import { buildSelectedWebSelection } from "./selectedWebSelection";

describe("buildSelectedWebSelection", () => {
  it("按 DOM 顺序保留权威纯文本、普通文本和原始图片 URL", () => {
    const result = buildSelectedWebSelection(
      '<article><p>文本 1</p><img src="https://example.com/a.png"><p>文本 2</p></article>',
      "文本 1\n文本 2"
    );

    expect(result).toEqual({
      version: 2,
      plain_text: "文本 1\n文本 2",
      parts: [
        { type: "text", text: "文本 1" },
        { type: "image_url", url: "https://example.com/a.png" },
        { type: "text", text: "文本 2" }
      ]
    });
  });

  it("把链接可见文字和目标 URL 保存在同一个 link part 中", () => {
    const result = buildSelectedWebSelection(
      '<p>请阅读 <a href="https://example.com/target?q=1#part">显示文字</a>。</p>',
      "请阅读 显示文字。"
    );

    expect(result.parts).toEqual([
      { type: "text", text: "请阅读" },
      { type: "link", text: "显示文字", url: "https://example.com/target?q=1#part" },
      { type: "text", text: "。" }
    ]);
  });

  it("图片链接即使没有可见文字也同时保留链接目标和图片", () => {
    const result = buildSelectedWebSelection(
      '<a href="https://example.com/article"><img src="https://example.com/cover.png"></a>',
      null
    );

    expect(result.parts).toEqual([
      { type: "link", text: null, url: "https://example.com/article" },
      { type: "image_url", url: "https://example.com/cover.png" }
    ]);
  });

  it("非法链接不进入元数据，但仍保留可见文字和合法图片", () => {
    const result = buildSelectedWebSelection(
      '<a href="javascript:alert(1)">保留文字<img src="https://example.com/cover.png"></a>',
      "保留文字"
    );

    expect(result.parts).toEqual([
      { type: "text", text: "保留文字" },
      { type: "image_url", url: "https://example.com/cover.png" }
    ]);
  });

  it("忽略脚本、样式和无效图片来源", () => {
    const result = buildSelectedWebSelection(
      '<style>hidden</style><p>正文</p><img src="file:///tmp/a.png"><script>ignored()</script>',
      "正文"
    );

    expect(result.parts).toEqual([{ type: "text", text: "正文" }]);
  });

  it("没有纯文本格式时明确写入 null", () => {
    const result = buildSelectedWebSelection('<a href="https://example.com">网页</a>', null);

    expect(result.plain_text).toBeNull();
  });

  it("拒绝超过协议上限的内容片段", () => {
    const html = Array.from(
      { length: 201 },
      (_, index) => `<a href="https://example.com/${index}">${index}</a>`
    ).join("");

    expect(() => buildSelectedWebSelection(html, "原文")).toThrow("网页选区内容片段数量超过限制");
  });
});
