import { existsSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import type { BrowserWindow } from "electron";
import type { WindowKind } from "../contracts/windowKinds";
import { applyWindowNavigationPolicy } from "./windowNavigationPolicy";

/**
 * WindowPreloadPaths 保存主进程为每个窗口装配的 preload 路径。
 * 当前三个窗口共用同一个 sandbox 构建产物，但调用方仍按窗口种类取值。
 */
export type WindowPreloadPaths = Record<WindowKind, string>;

/**
 * resolveWindowPreloadPaths 解析三个窗口共用的 sandbox preload 入口。
 *
 * @param mainModuleDir 当前 main 模块目录。
 * @returns 三个窗口对应的 preload 文件路径。
 */
export function resolveWindowPreloadPaths(mainModuleDir: string): WindowPreloadPaths {
  const sandboxPreloadPath = resolveFirstExistingPath([
    join(mainModuleDir, "../preload/sandbox.cjs")
  ]);

  return {
    avatar: sandboxPreloadPath,
    panel: sandboxPreloadPath,
    chat: sandboxPreloadPath
  };
}

/**
 * loadWindowContent 根据窗口种类加载对应 renderer 页面。
 *
 * @param window 目标 BrowserWindow。
 * @param windowKind 窗口种类。
 * @param mainModuleDir 当前 main 模块目录。
 * @returns 页面加载完成后的 Promise。
 */
export async function loadWindowContent(
  window: BrowserWindow,
  windowKind: WindowKind,
  mainModuleDir: string
): Promise<void> {
  const rendererUrl = process.env["ELECTRON_RENDERER_URL"];

  if (rendererUrl) {
    const trustedEntryUrl = new URL(
      `${windowKind}.html`,
      rendererUrl.endsWith("/") ? rendererUrl : `${rendererUrl}/`
    ).toString();
    applyWindowNavigationPolicy(window, trustedEntryUrl);
    await window.loadURL(trustedEntryUrl);
    return;
  }

  const htmlPath = resolveFirstExistingPath([
    join(mainModuleDir, `../../dist/renderer/${windowKind}.html`),
    join(mainModuleDir, `../../renderer/${windowKind}.html`)
  ]);

  applyWindowNavigationPolicy(window, pathToFileURL(htmlPath).toString());
  await window.loadFile(htmlPath);
}

/**
 * resolveFirstExistingPath 按候选顺序返回第一个存在的资源路径。
 *
 * @param candidates 候选路径列表。
 * @returns 第一个存在的路径。
 */
function resolveFirstExistingPath(candidates: string[]): string {
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return candidate;
    }
  }

  throw new Error(`Unable to resolve window asset path: ${candidates.join(", ")}`);
}
