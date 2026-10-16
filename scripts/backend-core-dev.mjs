import { spawn } from "node:child_process";
import { readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildSpawnSpec } from "./dev-process.mjs";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PNPM_COMMAND = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
// Console 目前唯一位于 console 目录之外、会被 Vite 直接打包的输入。
// 用相对路径声明，测试和实际仓库都能共享同一套过期判断规则。
const CONSOLE_EXTERNAL_BUILD_INPUTS = ["assets/icon.png"];

/**
 * 递归获取目录中最新文件修改时间。
 *
 * Args:
 *   directory: 要检查的目录。
 *
 * Returns:
 *   目录存在时返回最新修改时间戳；目录不存在时返回 0。
 */
export async function latestFileMtime(directory) {
  try {
    const entry = await stat(directory);
    if (entry.isFile()) return entry.mtimeMs;
  } catch (error) {
    if (error?.code === "ENOENT") return 0;
    throw error;
  }
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return 0;
    throw error;
  }

  let latest = 0;
  for (const entry of entries) {
    const filePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      latest = Math.max(latest, await latestFileMtime(filePath));
      continue;
    }
    if (!entry.isFile()) continue;
    latest = Math.max(latest, (await stat(filePath)).mtimeMs);
  }
  return latest;
}

/**
 * 读取一个必须存在且必须是普通文件的构建输入的修改时间。
 *
 * Args:
 *   filePath: 要检查的文件路径。
 *
 * Returns:
 *   普通文件的修改时间；路径不存在、父路径不是目录或目标不是普通文件时返回 null。
 */
export async function readRegularFileMtime(filePath) {
  try {
    const entry = await stat(filePath);
    return entry.isFile() ? entry.mtimeMs : null;
  } catch (error) {
    // 路径缺失和父路径被普通文件替代，都表示这个构建输入不可用，交给正式构建暴露真实错误。
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") return null;
    throw error;
  }
}

/**
 * 判断静态 Console 是否需要重新构建。
 *
 * Args:
 *   repositoryRoot: NearMate 仓库根目录。
 *
 * Returns:
 *   构建产物不存在、落后于任一构建输入，或必需的外部输入缺失/类型异常时返回 true。
 */
export async function shouldBuildConsole(repositoryRoot = REPOSITORY_ROOT) {
  const indexPath = path.join(repositoryRoot, "console", "dist", "index.html");
  const indexMtime = await readRegularFileMtime(indexPath);
  const consoleRoot = path.join(repositoryRoot, "console");
  const externalInputMtimes = await Promise.all(
    CONSOLE_EXTERNAL_BUILD_INPUTS.map(relativePath =>
      readRegularFileMtime(path.join(repositoryRoot, relativePath))
    )
  );

  // 外部输入缺失或不是普通文件时也必须触发一次正式构建，让 Vite 报出真实依赖错误，
  // 不能因为旧 dist 仍存在就继续静默提供过期品牌资源。
  if (externalInputMtimes.some(mtime => mtime === null)) return true;

  const sourceMtime = Math.max(
    await latestFileMtime(path.join(consoleRoot, "src")),
    await latestFileMtime(path.join(consoleRoot, "public")),
    // 这些文件会改变 Vite 的入口、依赖或生成结果，也必须纳入过期判断。
    ...await Promise.all([
      "index.html", "package.json", "vite.config.ts", "postcss.config.js",
      "tailwind.config.ts", "tsconfig.json",
    ].map(fileName => latestFileMtime(path.join(consoleRoot, fileName)))),
    ...externalInputMtimes,
  );
  return indexMtime === null || sourceMtime > indexMtime;
}

async function runChild(executable, args) {
  const spec = buildSpawnSpec(executable, args);
  return await new Promise(resolve => {
    const child = spawn(spec.executable, spec.args, {
      cwd: REPOSITORY_ROOT,
      env: process.env,
      stdio: "inherit",
      windowsHide: true,
      shell: spec.shell,
    });
    child.once("error", error => {
      console.error(`[backend] 子进程启动失败：${error.message}`);
      resolve(1);
    });
    child.once("exit", (code, signal) => resolve(signal ? 1 : (code ?? 0)));
  });
}

/**
 * 源码开发 Backend 启动入口。
 *
 * 启动前自动刷新 Backend 托管的 Console 静态产物，防止浏览器加载旧版轮询代码；随后把信号和退出码交给 Backend。
 *
 * Args:
 *   无；命令行参数会原样传给 companion-core。
 *
 * Returns:
 *   Backend 子进程退出码。
 */
export async function main(argv = process.argv.slice(2)) {
  if (await shouldBuildConsole()) {
    console.log("[backend] Console 构建产物已过期，正在自动执行 pnpm console:build…");
    const buildCode = await runChild(PNPM_COMMAND, ["console:build"]);
    if (buildCode !== 0) return buildCode;
  }

  const backendExecutable = process.platform === "win32"
    ? path.join(REPOSITORY_ROOT, ".venv", "Scripts", "companion-core.exe")
    : path.join(REPOSITORY_ROOT, ".venv", "bin", "companion-core");
  return await runChild(backendExecutable, argv);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then(code => { process.exitCode = code; }).catch(error => {
    console.error(`[backend] 启动失败：${error.message}`);
    process.exitCode = 1;
  });
}
