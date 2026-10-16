import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const rendererRoot = dirname(fileURLToPath(import.meta.url));

describe("renderer security policy", () => {
  it.each(["avatar", "panel", "chat"])("%s 窗口声明统一 CSP", windowKind => {
    const html = readFileSync(join(rendererRoot, `${windowKind}.html`), "utf8");

    expect(html).toContain('http-equiv="Content-Security-Policy"');
    expect(html).toContain("default-src 'self'");
    expect(html).toContain("object-src 'none'");
    expect(html).toContain("connect-src 'self' data:");
    expect(html).toContain("ws://127.0.0.1:*");
    expect(html).toContain("wss:");
    expect(html).not.toContain("'unsafe-eval'");
  });

  it("Avatar 在加载组件依赖前安装 Pixi 的 CSP 安全实现", () => {
    const avatarEntry = readFileSync(join(rendererRoot, "src", "avatar-main.tsx"), "utf8");
    const cspRuntimeImportIndex = avatarEntry.indexOf('import "pixi.js/unsafe-eval";');
    const avatarAppImportIndex = avatarEntry.indexOf('import { AvatarApp } from "./avatar/AvatarApp";');

    expect(cspRuntimeImportIndex).toBeGreaterThanOrEqual(0);
    expect(cspRuntimeImportIndex).toBeLessThan(avatarAppImportIndex);
  });
});
