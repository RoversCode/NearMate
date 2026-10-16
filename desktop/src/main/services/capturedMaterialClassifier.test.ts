import { describe, expect, it } from "vitest";
import { classifyCapturedMaterial } from "./capturedMaterialClassifier";

describe("capturedMaterialClassifier", () => {
  it("使用已提取的路径事实纯分类文件和文件夹", () => {
    const content = capturedClipboard({
      filePaths: ["G:\\docs\\a.md", "G:\\project"]
    });

    expect(classifyCapturedMaterial(content, new Set(["G:\\project"]))).toEqual({
      captured: {
        kind: "paths",
        items: [
          expect.objectContaining({ kind: "dragged_file", path: "G:\\docs\\a.md" }),
          expect.objectContaining({ kind: "dragged_folder", path: "G:\\project" })
        ]
      },
      diagnostics: {
        standaloneHttpUrl: false,
        linkCount: 0,
        imageCount: 0,
        fallbackReason: null
      }
    });
  });

  it("相同输入始终生成相同文本分类且不创建受管资产", () => {
    const content = capturedClipboard({ text: " hello " });

    expect(classifyCapturedMaterial(content)).toEqual(classifyCapturedMaterial(content));
    expect(classifyCapturedMaterial(content).captured).toEqual({ kind: "text", text: "hello" });
  });

  it("Edge 用标题链接包装地址栏 URL 时逐字保留纯文本 URL", () => {
    const originalUrl = "https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333.1007#reply42";
    const content = capturedClipboard({
      text: `  ${originalUrl}\r\n`,
      html: `<a href="${originalUrl}">大疆 vlog｜我在交大的读研生活_哔哩哔哩_bilibili</a>`
    });

    expect(classifyCapturedMaterial(content)).toEqual({
      captured: { kind: "text", text: originalUrl },
      diagnostics: {
        standaloneHttpUrl: true,
        linkCount: 1,
        imageCount: 0,
        fallbackReason: "standalone_url_matches_html_link"
      }
    });
  });

  it("只为同目标判断规范化 URL，不改写写入材料的原始 URL", () => {
    const originalUrl = "HTTPS://Example.COM:443";
    const content = capturedClipboard({
      text: originalUrl,
      html: '<a href="https://example.com/">标题</a>'
    });

    expect(classifyCapturedMaterial(content).captured).toEqual({ kind: "text", text: originalUrl });
  });

  it("普通网页链接同时保留权威原文、可见文字和目标 URL", () => {
    const content = capturedClipboard({
      text: "查看产品说明",
      html: '<p>查看<a href="https://example.com/manual">产品说明</a></p>'
    });

    expect(classifyCapturedMaterial(content).captured).toEqual({
      kind: "web",
      selection: {
        version: 2,
        plain_text: "查看产品说明",
        parts: [
          { type: "text", text: "查看" },
          { type: "link", text: "产品说明", url: "https://example.com/manual" }
        ]
      }
    });
  });

  it("显示 URL 与链接目标不同时保留两者而不是误判为同目标", () => {
    const content = capturedClipboard({
      text: "https://display.example/path?keep=1",
      html: '<a href="https://target.example/path?keep=1">https://display.example/path?keep=1</a>'
    });

    expect(classifyCapturedMaterial(content).captured.kind).toBe("web");
  });

  it("HTML 没有链接或图片时始终保存纯文本原文", () => {
    const content = capturedClipboard({
      text: "  第一行\n  第二行  ",
      html: "<p>被浏览器改写的第一行</p><p>被浏览器改写的第二行</p>"
    });

    expect(classifyCapturedMaterial(content)).toEqual({
      captured: { kind: "text", text: "第一行\n  第二行" },
      diagnostics: {
        standaloneHttpUrl: false,
        linkCount: 0,
        imageCount: 0,
        fallbackReason: "html_without_rich_metadata"
      }
    });
  });

  it("HTML 增强解析失败但存在纯文本时降级且不丢失原文", () => {
    const oversizedHtml = Array.from(
      { length: 201 },
      (_, index) => `<a href="https://example.com/${index}">${index}</a>`
    ).join("");
    const content = capturedClipboard({ text: "必须保留的原文", html: oversizedHtml });

    expect(classifyCapturedMaterial(content)).toEqual({
      captured: { kind: "text", text: "必须保留的原文" },
      diagnostics: {
        standaloneHttpUrl: false,
        linkCount: 0,
        imageCount: 0,
        fallbackReason: "html_enrichment_failed"
      }
    });
  });

  it("只有 HTML 普通文字时仍能降级为文本", () => {
    const content = capturedClipboard({ html: "<p>仅 HTML 正文</p>" });

    expect(classifyCapturedMaterial(content).captured).toEqual({ kind: "text", text: "仅 HTML 正文" });
  });
});

function capturedClipboard(overrides: Partial<{
  filePaths: string[];
  text: string;
  html: string;
  imagePng: Buffer | null;
}>) {
  return {
    filePaths: overrides.filePaths ?? [],
    clipboardChanged: true,
    elapsedMs: 42,
    text: overrides.text ?? "",
    html: overrides.html ?? "",
    imagePng: overrides.imagePng ?? null
  };
}
