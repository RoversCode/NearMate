import { beforeEach, describe, expect, it } from "vitest";

import {
  applyNearMateConsoleBranding,
  NEARMATE_BRAND_ICON_URL,
  NEARMATE_CONSOLE_TITLE,
  NEARMATE_PRODUCT_NAME
} from "./brandIdentity";

describe("NearMate Console brand identity", () => {
  beforeEach(() => {
    document.head.innerHTML = "";
    document.title = "旧标题";
  });

  it("公开正式产品名、Console 标题和构建后的图标 URL", () => {
    expect(NEARMATE_PRODUCT_NAME).toBe("NearMate");
    expect(NEARMATE_CONSOLE_TITLE).toBe("NearMate Console");
    expect(NEARMATE_BRAND_ICON_URL).toContain("icon");
  });

  it("重复初始化时只保留一个 NearMate favicon", () => {
    const staleFavicon = document.createElement("link");
    staleFavicon.rel = "icon";
    staleFavicon.href = "/old-icon.png";
    document.head.appendChild(staleFavicon);

    const duplicateFavicon = document.createElement("link");
    duplicateFavicon.rel = "icon";
    duplicateFavicon.href = "/duplicate-icon.png";
    document.head.appendChild(duplicateFavicon);

    applyNearMateConsoleBranding(document);
    applyNearMateConsoleBranding(document);

    const faviconLinks = document.head.querySelectorAll('link[rel~="icon"]');
    expect(faviconLinks).toHaveLength(1);
    expect(faviconLinks[0].getAttribute("data-nearmate-favicon")).toBe("true");
    expect(faviconLinks[0].getAttribute("href")).toBe(NEARMATE_BRAND_ICON_URL);
    expect(document.title).toBe(NEARMATE_CONSOLE_TITLE);
  });
});
