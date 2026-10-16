import { parseFragment } from "parse5";

import { isHttpUrl } from "./httpUrl";

type SelectedWebSelectionPart =
  | { type: "text"; text: string }
  | { type: "link"; text: string | null; url: string }
  | { type: "image_url"; url: string };

export type SelectedWebSelection = {
  version: 2;
  plain_text: string | null;
  parts: SelectedWebSelectionPart[];
};

type HtmlNode = {
  nodeName: string;
  value?: string;
  attrs?: Array<{ name: string; value: string }>;
  childNodes?: HtmlNode[];
};

const MAX_SELECTED_WEB_PARTS = 200;
const IGNORED_ELEMENT_NAMES = new Set(["script", "style", "noscript", "template"]);
const BLOCK_ELEMENT_NAMES = new Set([
  "address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figcaption", "figure", "footer",
  "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
  "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul"
]);

/**
 * 按 DOM 顺序把网页选区转换为权威纯文本与富格式补充信息。
 *
 * `plainText` 来自剪贴板的 `text/plain`，是用户选中原文。HTML 只负责补充链接和图片，
 * 不能用浏览器生成的标题或重排文本覆盖这份原文。
 *
 * @param html 剪贴板提供的网页选区 HTML。
 * @param plainText 剪贴板提供的完整纯文本；没有该格式时传 null。
 * @returns 可直接落盘并交给 Knowledge 的 v2 有序内容。
 */
export function buildSelectedWebSelection(
  html: string,
  plainText: string | null
): SelectedWebSelection {
  const root = parseFragment(html) as unknown as HtmlNode;
  const parts: SelectedWebSelectionPart[] = [];
  let textBuffer = "";

  const pushPart = (part: SelectedWebSelectionPart) => {
    parts.push(part);
    if (parts.length > MAX_SELECTED_WEB_PARTS) {
      throw new Error("网页选区内容片段数量超过限制");
    }
  };
  const flushText = () => {
    const text = normalizeText(textBuffer);
    textBuffer = "";
    if (text) pushPart({ type: "text", text });
  };
  const appendBlockBoundary = () => {
    if (textBuffer && !textBuffer.endsWith("\n")) textBuffer += "\n";
  };
  const visit = (node: HtmlNode) => {
    if (node.nodeName === "#text") {
      textBuffer += node.value ?? "";
      return;
    }
    if (IGNORED_ELEMENT_NAMES.has(node.nodeName)) return;
    if (node.nodeName === "br") {
      appendBlockBoundary();
      return;
    }
    if (node.nodeName === "img") {
      flushText();
      const source = readAttribute(node, "src");
      if (source && isHttpUrl(source)) pushPart({ type: "image_url", url: source });
      return;
    }
    if (node.nodeName === "a") {
      flushText();
      for (const part of buildAnchorParts(node)) pushPart(part);
      return;
    }

    const isBlock = BLOCK_ELEMENT_NAMES.has(node.nodeName);
    if (isBlock) appendBlockBoundary();
    for (const child of node.childNodes ?? []) visit(child);
    if (isBlock) appendBlockBoundary();
  };

  visit(root);
  flushText();
  const normalizedPlainText = plainText?.trim() || null;
  return { version: 2, plain_text: normalizedPlainText, parts };
}

/**
 * 把一个链接节点拆成仍然保持 DOM 顺序的文字和图片片段。
 *
 * @param anchor 当前 `<a>` 节点。
 * @returns 合法目标使用 link part；非法目标降级为普通文字，但图片仍然保留。
 */
function buildAnchorParts(anchor: HtmlNode): SelectedWebSelectionPart[] {
  const href = readAttribute(anchor, "href");
  const validHref = href && isHttpUrl(href) ? href : null;
  const rawParts: Array<{ type: "text"; text: string } | { type: "image_url"; url: string }> = [];
  let textBuffer = "";

  const flushText = () => {
    const text = normalizeText(textBuffer);
    textBuffer = "";
    if (text) rawParts.push({ type: "text", text });
  };
  const appendBlockBoundary = () => {
    if (textBuffer && !textBuffer.endsWith("\n")) textBuffer += "\n";
  };
  const visit = (node: HtmlNode) => {
    if (node.nodeName === "#text") {
      textBuffer += node.value ?? "";
      return;
    }
    if (IGNORED_ELEMENT_NAMES.has(node.nodeName)) return;
    if (node.nodeName === "br") {
      appendBlockBoundary();
      return;
    }
    if (node.nodeName === "img") {
      flushText();
      const source = readAttribute(node, "src");
      if (source && isHttpUrl(source)) rawParts.push({ type: "image_url", url: source });
      return;
    }

    const isBlock = BLOCK_ELEMENT_NAMES.has(node.nodeName);
    if (isBlock) appendBlockBoundary();
    for (const child of node.childNodes ?? []) visit(child);
    if (isBlock) appendBlockBoundary();
  };

  for (const child of anchor.childNodes ?? []) visit(child);
  flushText();
  if (!validHref) return rawParts;

  const linkedParts: SelectedWebSelectionPart[] = rawParts.map(part => (
    part.type === "text"
      ? { type: "link", text: part.text, url: validHref }
      : part
  ));
  if (!linkedParts.some(part => part.type === "link")) {
    linkedParts.unshift({ type: "link", text: null, url: validHref });
  }
  return linkedParts;
}

/**
 * 读取并清理 HTML 属性值。
 *
 * @param node 需要读取属性的节点。
 * @param attributeName 属性名。
 * @returns 去掉首尾空白的属性值；不存在或为空时返回 null。
 */
function readAttribute(node: HtmlNode, attributeName: string): string | null {
  return node.attrs?.find(attribute => attribute.name.toLowerCase() === attributeName)?.value.trim() || null;
}

/**
 * 归一化 DOM 文本空白，同时保留块级换行。
 *
 * 这里只整理 HTML 补充信息，不会处理或覆盖权威 `plain_text`。
 *
 * @param value 尚未整理的文本缓冲。
 * @returns 可写入 JSON part 的稳定正文。
 */
function normalizeText(value: string): string {
  return value
    .replace(/\r\n?/g, "\n")
    .replace(/[^\S\n]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
