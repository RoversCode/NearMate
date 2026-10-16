import nearMateIconUrl from "../../../assets/icon.png";

/** NearMate 的正式产品名称。 */
export const NEARMATE_PRODUCT_NAME = "NearMate";

/** Console 在浏览器标签和初始化外壳中使用的正式标题。 */
export const NEARMATE_CONSOLE_TITLE = `${NEARMATE_PRODUCT_NAME} Console`;

/** 由 Vite 构建后的 NearMate 品牌图标 URL。 */
export const NEARMATE_BRAND_ICON_URL = nearMateIconUrl;

const FAVICON_SELECTOR = 'link[rel~="icon"]';
const BRAND_FAVICON_ATTRIBUTE = "data-nearmate-favicon";

/**
 * 设置 Console 的产品标题和 favicon。
 *
 * 入口在 React 挂载前调用这个函数，保证首屏就使用正式产品名；函数可以重复调用，
 * 因为 React StrictMode 或热更新都可能让入口初始化不止一次。
 *
 * Args:
 *   documentRef: 要更新的文档，默认使用当前浏览器文档；测试可以传入独立文档。
 *
 * Returns:
 *   无返回值；目标文档会只保留一个 NearMate favicon link。
 */
export function applyNearMateConsoleBranding(documentRef: Document = window.document): void {
  documentRef.title = NEARMATE_CONSOLE_TITLE;

  const existingFaviconLinks = Array.from(documentRef.head.querySelectorAll<HTMLLinkElement>(FAVICON_SELECTOR));
  const faviconLink =
    existingFaviconLinks.find(link => link.getAttribute(BRAND_FAVICON_ATTRIBUTE) === "true") ??
    existingFaviconLinks[0] ??
    documentRef.createElement("link");

  // 旧 link 可能由热更新或历史入口留下；先移除多余节点，避免浏览器在多个图标之间自行选择。
  for (const link of existingFaviconLinks) {
    if (link !== faviconLink) {
      link.remove();
    }
  }

  faviconLink.rel = "icon";
  faviconLink.type = "image/png";
  faviconLink.href = NEARMATE_BRAND_ICON_URL;
  faviconLink.setAttribute(BRAND_FAVICON_ATTRIBUTE, "true");

  if (!faviconLink.isConnected) {
    documentRef.head.appendChild(faviconLink);
  }
}
