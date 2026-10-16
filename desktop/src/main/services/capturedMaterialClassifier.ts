import { win32 } from "node:path";

import type { MaterialItemPayload } from "./materialsClient";
import type { CapturedClipboardContent } from "./selectionClipboardTransaction";
import { areEquivalentHttpUrls, isHttpUrl } from "./httpUrl";
import {
  buildSelectedWebSelection,
  type SelectedWebSelection
} from "./selectedWebSelection";

export type CapturedMaterial =
  | { kind: "paths"; items: MaterialItemPayload[] }
  | { kind: "web"; selection: Record<string, unknown> }
  | { kind: "text"; text: string }
  | { kind: "image"; png: Buffer }
  | { kind: "empty" };

type CapturedMaterialDiagnostics = {
  standaloneHttpUrl: boolean;
  linkCount: number;
  imageCount: number;
  fallbackReason:
    | "standalone_url_matches_html_link"
    | "html_without_rich_metadata"
    | "html_enrichment_failed"
    | null;
};

export type CapturedMaterialClassification = {
  captured: CapturedMaterial;
  diagnostics: CapturedMaterialDiagnostics;
};

/**
 * 纯函数式分类一次复制结果，并同时生成不含用户内容的诊断事实。
 *
 * 文件路径保持最高优先级。纯文本是用户选中原文，HTML 只能补充链接和图片，不能用
 * 浏览器生成的标题或重排文本覆盖纯文本。
 *
 * @param content 剪贴板事务取得的原始复制内容。
 * @param directoryPaths 已确认属于目录的路径集合。
 * @returns 不执行落盘和资产登记的分类结果与脱敏诊断。
 */
export function classifyCapturedMaterial(
  content: CapturedClipboardContent,
  directoryPaths: ReadonlySet<string> = new Set()
): CapturedMaterialClassification {
  const filePaths = normalizeFilePaths(content.filePaths);
  if (filePaths.length > 0) {
    return {
      captured: {
        kind: "paths",
        items: filePaths.map(path => ({
          kind: directoryPaths.has(path) ? "dragged_folder" : "dragged_file",
          source: "selection_capture",
          display_name: win32.basename(path) || path,
          path
        }))
      },
      diagnostics: emptyDiagnostics()
    };
  }

  const plainText = content.text.trim();
  const standaloneHttpUrl = isHttpUrl(plainText);
  const html = content.html.trim();
  if (html) {
    try {
      const selection = buildSelectedWebSelection(html, plainText || null);
      const linkCount = selection.parts.filter(part => part.type === "link").length;
      const imageCount = selection.parts.filter(part => part.type === "image_url").length;
      const diagnostics = {
        standaloneHttpUrl,
        linkCount,
        imageCount,
        fallbackReason: null
      } satisfies CapturedMaterialDiagnostics;

      // 地址栏 URL 常被浏览器包装成标题链接。两者目标相同时，原始 URL 才是用户选中的内容。
      if (standaloneHttpUrl && isSingleEquivalentLink(selection, plainText)) {
        return {
          captured: { kind: "text", text: plainText },
          diagnostics: {
            ...diagnostics,
            fallbackReason: "standalone_url_matches_html_link"
          }
        };
      }

      // 只有链接或图片才需要网页选区协议；普通富文本继续使用更直接的文本材料。
      if (linkCount > 0 || imageCount > 0) {
        return { captured: { kind: "web", selection }, diagnostics };
      }

      const fallbackText = plainText || visibleHtmlText(selection);
      if (fallbackText) {
        return {
          captured: { kind: "text", text: fallbackText },
          diagnostics: {
            ...diagnostics,
            fallbackReason: "html_without_rich_metadata"
          }
        };
      }
    } catch {
      // HTML 是可选增强。解析或协议限额失败时，必须保住用户的纯文本原文。
      if (plainText) {
        return {
          captured: { kind: "text", text: plainText },
          diagnostics: {
            standaloneHttpUrl,
            linkCount: 0,
            imageCount: 0,
            fallbackReason: "html_enrichment_failed"
          }
        };
      }
    }
  }

  if (plainText) {
    return {
      captured: { kind: "text", text: plainText },
      diagnostics: { ...emptyDiagnostics(), standaloneHttpUrl }
    };
  }
  if (content.imagePng?.byteLength) {
    return {
      captured: { kind: "image", png: content.imagePng },
      diagnostics: emptyDiagnostics()
    };
  }
  return { captured: { kind: "empty" }, diagnostics: emptyDiagnostics() };
}

/**
 * 创建没有富格式事实时的统一诊断值。
 *
 * @returns 不包含用户正文、URL 或路径的空诊断。
 */
function emptyDiagnostics(): CapturedMaterialDiagnostics {
  return {
    standaloneHttpUrl: false,
    linkCount: 0,
    imageCount: 0,
    fallbackReason: null
  };
}

/**
 * 判断 HTML 是否只是把一个地址栏 URL 换成了标题展示。
 *
 * @param selection 已解析的网页选区。
 * @param plainText 用户选中的完整纯文本 URL。
 * @returns HTML 只有一个同目标链接且没有其他内容时返回 true。
 */
function isSingleEquivalentLink(selection: SelectedWebSelection, plainText: string): boolean {
  if (selection.parts.length !== 1) return false;
  const [part] = selection.parts;
  return part.type === "link" && areEquivalentHttpUrls(part.url, plainText);
}

/**
 * 在剪贴板没有纯文本格式时，从 HTML 的可见文字生成保底文本。
 *
 * @param selection 已解析的网页选区。
 * @returns 按片段分行拼接的可见文字。
 */
function visibleHtmlText(selection: SelectedWebSelection): string {
  return selection.parts.flatMap(part => {
    if (part.type === "text") return [part.text];
    if (part.type === "link" && part.text) return [part.text];
    return [];
  }).join("\n").trim();
}

/**
 * 清理和去重操作系统返回的文件路径。
 *
 * @param filePaths 剪贴板中的原始路径列表。
 * @returns 保留首次出现顺序的非空路径。
 */
function normalizeFilePaths(filePaths: string[]): string[] {
  const seen = new Set<string>();
  return filePaths.flatMap(path => {
    const normalized = path.trim();
    const key = normalized.toLowerCase();
    if (!normalized || seen.has(key)) return [];
    seen.add(key);
    return [normalized];
  });
}
