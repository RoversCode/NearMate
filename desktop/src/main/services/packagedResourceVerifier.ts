import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { WINDOW_KINDS } from "../contracts/windowKinds";
import {
  resolveNearMateRuntimeLayout,
  type NearMateRuntimeLayout,
  type RuntimeManifestFileRecord
} from "./runtimeLayout";

type FileCheckFailure = {
  path: string;
  reason: string;
};

/**
 * assertPackagedResourcesIntact 在安装态启动前验证随包关键资源完整。
 *
 * 清单中的 required_files 是启动真正会立刻用到的最小集合。这里同时检查
 * 普通文件类型、文件大小和 SHA256；这样“把可执行文件替换成目录”或“同名文件
 * 被悄悄改写”的损坏会在创建窗口和启动 Backend 之前被发现。清单里的其它文件
 * 仍由构建阶段完整生成摘要，启动阶段不再次读取数 GB 的模型，避免每次启动都
 * 被完整性扫描拖住。
 *
 * Args:
 *   input: 安装资源的 resourcesPath 与当前 main 模块目录。
 *
 * Returns:
 *   无；任一关键资源缺失、不是普通文件、经过重解析路径或摘要不符时抛错。
 */
export function assertPackagedResourcesIntact(input: {
  resourcesPath: string;
  mainModuleDir: string;
}): void {
  const brokenPaths: FileCheckFailure[] = [];
  let layout: NearMateRuntimeLayout | null = null;

  try {
    layout = resolveNearMateRuntimeLayout({ isPackaged: true, resourcesPath: input.resourcesPath });
  } catch (error) {
    brokenPaths.push({
      path: join(input.resourcesPath, "runtime", "runtime-manifest.json"),
      reason: error instanceof Error ? error.message : "清单无法解析"
    });
  }

  if (layout) {
    verifyRequiredRuntimeFiles(layout, brokenPaths);
  }

  // Electron 主程序自身的 renderer/preload 资源不在 runtime 清单根目录，单独检查，
  // 但仍要求它们是真正的普通文件，不能让目录或失效链接混过启动门禁。
  for (const kind of WINDOW_KINDS) {
    const htmlPath = join(input.mainModuleDir, "..", "..", "dist", "renderer", `${kind}.html`);
    verifyOrdinaryFile(htmlPath, "renderer 页面", brokenPaths);
  }

  const preloadPath = join(input.mainModuleDir, "..", "preload", "sandbox.cjs");
  verifyOrdinaryFile(preloadPath, "preload", brokenPaths);

  const trayIconPath = join(
    layout?.assetsRoot ?? join(input.resourcesPath, "runtime", "assets"),
    "icon.png"
  );
  verifyOrdinaryFile(trayIconPath, "托盘图标", brokenPaths);

  if (brokenPaths.length > 0) {
    const listing = brokenPaths
      .map(({ path, reason }) => `· ${path}${reason ? `（${reason}）` : ""}`)
      .join("\n");
    throw new Error(`NearMate 安装资源损坏或缺失：\n${listing}\n请重新安装 NearMate。`);
  }
}

/**
 * 核对 manifest 声明的启动必需文件。
 *
 * Args:
 *   layout: 已通过 RuntimeLayout 结构校验的安装布局。
 *   brokenPaths: 收集所有失败项的输出数组。
 * Returns:
 *   无；失败项追加到 brokenPaths，不在第一项失败时丢掉其它诊断。
 */
function verifyRequiredRuntimeFiles(
  layout: NearMateRuntimeLayout,
  brokenPaths: FileCheckFailure[]
): void {
  const records = new Map<string, RuntimeManifestFileRecord>();
  for (const record of layout.manifest?.files ?? []) {
    records.set(record.path.toLowerCase(), record);
  }

  for (const relativePath of layout.manifest?.required_files ?? []) {
    const record = records.get(relativePath.toLowerCase());
    const absolutePath = join(layout.runtimeRoot, relativePath);
    if (!record) {
      brokenPaths.push({ path: absolutePath, reason: "未登记 SHA256 摘要" });
      continue;
    }
    verifyManifestFile(layout.runtimeRoot, relativePath, record, brokenPaths);
  }
}

/**
 * 校验一个清单文件的路径边界、普通文件属性、大小和摘要。
 *
 * Args:
 *   runtimeRoot: 清单根目录。
 *   relativePath: 清单中的正斜杠相对路径。
 *   record: 清单登记的大小和 SHA256。
 *   brokenPaths: 收集失败项的输出数组。
 * Returns:
 *   无。
 */
function verifyManifestFile(
  runtimeRoot: string,
  relativePath: string,
  record: RuntimeManifestFileRecord,
  brokenPaths: FileCheckFailure[]
): void {
  const absolutePath = join(runtimeRoot, relativePath);
  const pathFailure = verifyRuntimePathAncestors(runtimeRoot, relativePath);
  if (pathFailure) {
    brokenPaths.push({ path: absolutePath, reason: pathFailure });
    return;
  }

  let stats: ReturnType<typeof lstatSync>;
  try {
    stats = lstatSync(absolutePath);
  } catch {
    brokenPaths.push({ path: absolutePath, reason: "文件不存在" });
    return;
  }
  if (!stats.isFile()) {
    brokenPaths.push({ path: absolutePath, reason: "不是普通文件" });
    return;
  }
  if (stats.size !== record.size) {
    brokenPaths.push({
      path: absolutePath,
      reason: `文件大小不符（期望 ${record.size}，实际 ${stats.size}）`
    });
    return;
  }

  try {
    const actualHash = createHash("sha256").update(readFileSync(absolutePath)).digest("hex");
    if (actualHash.toLowerCase() !== record.sha256.toLowerCase()) {
      brokenPaths.push({ path: absolutePath, reason: "SHA256 摘要不符" });
    }
  } catch {
    brokenPaths.push({ path: absolutePath, reason: "文件无法读取" });
  }
}

/**
 * 校验 Electron 资源是普通文件。
 *
 * Args:
 *   path: 需要检查的绝对路径。
 *   description: 面向用户的资源名称。
 *   brokenPaths: 收集失败项的输出数组。
 * Returns:
 *   无。
 */
function verifyOrdinaryFile(
  path: string,
  description: string,
  brokenPaths: FileCheckFailure[]
): void {
  try {
    const stats = lstatSync(path);
    if (!stats.isFile() || stats.isSymbolicLink()) {
      brokenPaths.push({ path, reason: `${description}不是普通文件` });
    }
  } catch {
    brokenPaths.push({ path, reason: `${description}不存在` });
  }
}

/**
 * 逐级检查 runtime 路径，拒绝符号链接、目录联接和其它重解析路径。
 *
 * Args:
 *   runtimeRoot: 清单根目录。
 *   relativePath: 清单中的正斜杠相对路径。
 * Returns:
 *   null 表示安全；否则返回失败原因。
 */
function verifyRuntimePathAncestors(runtimeRoot: string, relativePath: string): string | null {
  const root = resolve(runtimeRoot);
  let current = root;
  for (const part of relativePath.split("/")) {
    current = join(current, part);
    let stats: ReturnType<typeof lstatSync>;
    try {
      stats = lstatSync(current);
    } catch {
      return "文件不存在";
    }
    if (stats.isSymbolicLink()) {
      return "路径包含符号链接或目录联接";
    }
    try {
      const actual = realpathSync.native(current);
      if (canonicalPath(actual) !== canonicalPath(current)) {
        return "路径包含重解析点";
      }
    } catch {
      return "路径无法解析";
    }
  }

  const relativeToRoot = relative(canonicalPath(root), canonicalPath(current));
  if (relativeToRoot === ".." || relativeToRoot.startsWith(`..${sep}`) || isAbsolute(relativeToRoot)) {
    return "路径超出 runtime 根目录";
  }
  return null;
}

function canonicalPath(path: string): string {
  const normalized = resolve(path);
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}
