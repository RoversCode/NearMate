import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import { access, cp, mkdir, readdir, readFile, rm, stat } from "node:fs/promises";
import { constants as fsConstants } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { spawn } from "node:child_process";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";

// Windows 本地缓存目录固定放在仓库内，业务意图是避免修改用户系统 PATH 或依赖管理员权限。
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const WINDOWS_CACHE_DIR = join(REPO_ROOT, "livekit_server");
const WINDOWS_EXECUTABLE_NAME = "livekit-server.exe";
const LIVEKIT_SERVER_COMMAND = "livekit-server";
const DEFAULT_LIVEKIT_SERVER_VERSION = "1.12.0";

/**
 * getLiveKitServerVersion 读取本地开发使用的 LiveKit Server 版本。
 * 核心入参为空；预期输出是版本字符串；边界异常是环境变量为空字符串时回退到仓库约定版本。
 */
function getLiveKitServerVersion() {
  return process.env.LIVEKIT_SERVER_VERSION?.trim() || DEFAULT_LIVEKIT_SERVER_VERSION;
}

/**
 * getWindowsDownloadUrl 生成 Windows amd64 版本的 LiveKit Server 下载地址。
 * 核心入参是版本号；预期输出是 GitHub release zip URL；边界异常是用户可通过 LIVEKIT_SERVER_DOWNLOAD_URL 覆盖下载源。
 */
function getWindowsDownloadUrl(version) {
  return process.env.LIVEKIT_SERVER_DOWNLOAD_URL?.trim()
    || `https://github.com/livekit/livekit/releases/download/v${version}/livekit_${version}_windows_amd64.zip`;
}

/**
 * pathExists 判断路径是否存在。
 * 核心入参是文件或目录路径；预期输出是布尔值；边界异常是权限或不存在都按不可用处理。
 */
async function pathExists(path) {
  try {
    await access(path, fsConstants.F_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * runCommand 执行短生命周期命令并收集输出。
 * 核心入参是命令、参数和可选超时；预期输出包含退出码、信号、stdout、stderr；边界异常是启动失败时返回 error。
 */
function runCommand(command, args, { timeoutMs = 10_000 } = {}) {
  return new Promise(resolveResult => {
    const child = spawn(command, args, {
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
      windowsHide: true,
    });

    let stdout = "";
    let stderr = "";
    let settled = false;

    // 超时保护用于验证可执行文件是否能快速响应，避免坏文件让启动流程卡死。
    const timeout = setTimeout(() => {
      if (settled) {
        return;
      }

      settled = true;
      child.kill("SIGTERM");
      resolveResult({ code: null, signal: "TIMEOUT", stdout, stderr, error: null });
    }, timeoutMs);

    child.stdout?.on("data", chunk => {
      stdout += chunk.toString();
    });

    child.stderr?.on("data", chunk => {
      stderr += chunk.toString();
    });

    child.on("error", error => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timeout);
      resolveResult({ code: null, signal: null, stdout, stderr, error });
    });

    child.on("close", (code, signal) => {
      if (settled) {
        return;
      }

      settled = true;
      clearTimeout(timeout);
      resolveResult({ code, signal, stdout, stderr, error: null });
    });
  });
}

/**
 * isUsableLiveKitServer 验证候选 livekit-server 是否可执行。
 * 核心入参是候选可执行文件路径；预期输出是布尔值；边界异常是 --version 不可用时再尝试 --help，避免误判旧版本。
 */
async function isUsableLiveKitServer(executablePath) {
  const versionResult = await runCommand(executablePath, ["--version"]);

  if (versionResult.code === 0) {
    return true;
  }

  const helpResult = await runCommand(executablePath, ["--help"]);
  return helpResult.code === 0;
}

/**
 * resolveFromExplicitPath 处理用户显式指定的 LIVEKIT_SERVER_PATH。
 * 核心入参为空；预期输出是可执行文件路径或 null；边界异常是路径不存在或不可执行时直接返回 null 并输出中文提示。
 */
async function resolveFromExplicitPath() {
  const explicitPath = process.env.LIVEKIT_SERVER_PATH?.trim();

  if (!explicitPath) {
    return null;
  }

  const resolvedPath = resolve(explicitPath);

  if (!await pathExists(resolvedPath)) {
    console.error(`[livekit:dev] LIVEKIT_SERVER_PATH 指向的文件不存在：${resolvedPath}`);
    return null;
  }

  if (!await isUsableLiveKitServer(resolvedPath)) {
    console.error(`[livekit:dev] LIVEKIT_SERVER_PATH 指向的文件不可用：${resolvedPath}`);
    return null;
  }

  return resolvedPath;
}

/**
 * resolveFromWindowsPath 在 Windows PATH 中查找 livekit-server。
 * 核心入参为空；预期输出是可执行文件路径或 null；边界异常是 where 不可用或候选不可执行时继续尝试项目内缓存。
 */
async function resolveFromWindowsPath() {
  const lookup = await runCommand("where", [LIVEKIT_SERVER_COMMAND]);

  if (lookup.error || lookup.code !== 0) {
    return null;
  }

  // 只取 PATH 查找顺序中的第一个可用候选，业务意图是尊重用户显式安装的版本。
  const candidates = lookup.stdout.split(/\r?\n/).map(line => line.trim()).filter(Boolean);

  for (const candidate of candidates) {
    if (await isUsableLiveKitServer(candidate)) {
      return candidate;
    }
  }

  return null;
}

/**
 * getCachedWindowsExecutablePath 返回仓库内缓存的 Windows 可执行文件路径。
 * 核心入参为空；预期输出是固定缓存路径；边界异常无。
 */
function getCachedWindowsExecutablePath() {
  return join(WINDOWS_CACHE_DIR, WINDOWS_EXECUTABLE_NAME);
}

/**
 * findExtractedLiveKitExecutable 在解压目录内查找 livekit-server.exe。
 * 核心入参是解压目录路径；预期输出是找到的 exe 路径或 null；边界异常是目录读取失败时向上抛出。
 */
async function findExtractedLiveKitExecutable(root) {
  const entries = await readdir(root, { withFileTypes: true });

  for (const entry of entries) {
    const entryPath = join(root, entry.name);

    if (entry.isFile() && entry.name.toLowerCase() === WINDOWS_EXECUTABLE_NAME) {
      return entryPath;
    }

    if (entry.isDirectory()) {
      const nested = await findExtractedLiveKitExecutable(entryPath);

      if (nested) {
        return nested;
      }
    }
  }

  return null;
}

/**
 * downloadFile 下载远程文件到本地路径。
 * 核心入参是 URL 与目标路径；预期输出是写入完成的 zip 文件；边界异常是 HTTP 非 2xx 或网络失败时抛出错误。
 */
async function downloadFile(url, destinationPath) {
  const response = await fetch(url);

  if (!response.ok || !response.body) {
    throw new Error(`下载失败：HTTP ${response.status} ${response.statusText}`);
  }

  await pipeline(Readable.fromWeb(response.body), createWriteStream(destinationPath));
}

/**
 * verifyOptionalSha256 在用户提供校验值时验证下载文件。
 * 核心入参是文件路径；预期输出为空；边界异常是校验不一致时抛出错误，避免继续使用损坏下载。
 */
async function verifyOptionalSha256(filePath) {
  const expected = process.env.LIVEKIT_SERVER_SHA256?.trim().toLowerCase();

  if (!expected) {
    return;
  }

  const fileBuffer = await readFile(filePath);
  const actual = createHash("sha256").update(fileBuffer).digest("hex");

  if (actual !== expected) {
    throw new Error(`LiveKit Server zip SHA256 校验失败，期望 ${expected}，实际 ${actual}`);
  }
}

/**
 * quotePowerShellString 把普通路径转换为 PowerShell 单引号字符串。
 * 核心入参是 Windows 路径；预期输出是可嵌入 -Command 的字面量；边界异常是路径中包含单引号时按 PowerShell 规则翻倍转义。
 */
function quotePowerShellString(value) {
  return `'${value.replaceAll("'", "''")}'`;
}

/**
 * extractZipWithPowerShell 使用 Windows 自带 PowerShell 解压 zip。
 * 核心入参是 zip 文件路径和目标目录；预期输出是已解压目录；边界异常是 PowerShell 不可用或解压失败时抛出错误。
 */
async function extractZipWithPowerShell(zipPath, destinationDir) {
  const result = await runCommand("powershell", [
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
    `Expand-Archive -LiteralPath ${quotePowerShellString(zipPath)} -DestinationPath ${quotePowerShellString(destinationDir)} -Force`,
  ], { timeoutMs: 120_000 });

  if (result.error) {
    throw result.error;
  }

  if (result.code !== 0) {
    throw new Error(result.stderr || result.stdout || `PowerShell 解压失败，退出码 ${result.code}`);
  }
}

/**
 * downloadWindowsLiveKitServer 下载并安装 Windows 本地缓存版本。
 * 核心入参为空；预期输出是项目内 livekit-server.exe 路径；边界异常是网络、校验、解压或可执行验证失败时抛出错误。
 */
async function downloadWindowsLiveKitServer() {
  const version = getLiveKitServerVersion();
  const downloadUrl = getWindowsDownloadUrl(version);
  const executablePath = getCachedWindowsExecutablePath();
  const tempRoot = join(WINDOWS_CACHE_DIR, `.download-${Date.now()}`);
  const zipPath = join(tempRoot, basename(new URL(downloadUrl).pathname) || `livekit_${version}_windows_amd64.zip`);
  const extractDir = join(tempRoot, "extracted");

  await mkdir(tempRoot, { recursive: true });
  await mkdir(extractDir, { recursive: true });

  try {
    console.log(`[livekit:dev] Windows 未找到可用 livekit-server，开始下载 LiveKit Server ${version}。`);
    console.log(`[livekit:dev] 下载地址：${downloadUrl}`);

    await downloadFile(downloadUrl, zipPath);
    await verifyOptionalSha256(zipPath);
    await extractZipWithPowerShell(zipPath, extractDir);

    const extractedExecutable = await findExtractedLiveKitExecutable(extractDir);

    if (!extractedExecutable) {
      throw new Error("解压结果中没有找到 livekit-server.exe");
    }

    await mkdir(WINDOWS_CACHE_DIR, { recursive: true });
    await cp(extractedExecutable, executablePath, { force: true });

    // 读取文件信息用于确认复制完成，避免后续验证遇到半写入文件。
    await stat(executablePath);

    if (!await isUsableLiveKitServer(executablePath)) {
      throw new Error(`下载后的 livekit-server.exe 无法执行：${executablePath}`);
    }

    console.log(`[livekit:dev] LiveKit Server 已缓存到 ${executablePath}`);
    return executablePath;
  } finally {
    await rm(tempRoot, { recursive: true, force: true });
  }
}

/**
 * resolveFromWindowsCache 查找项目内缓存的 livekit-server.exe。
 * 核心入参为空；预期输出是可执行文件路径或 null；边界异常是文件存在但不可用时删除后允许后续重新下载。
 */
async function resolveFromWindowsCache() {
  const executablePath = getCachedWindowsExecutablePath();

  if (!await pathExists(executablePath)) {
    return null;
  }

  if (await isUsableLiveKitServer(executablePath)) {
    return executablePath;
  }

  console.error(`[livekit:dev] 项目内缓存的 livekit-server.exe 不可用，将重新下载：${executablePath}`);
  await rm(executablePath, { force: true });
  return null;
}

/**
 * ensureLiveKitServerExecutable 解析或准备 livekit-server 可执行文件。
 * 核心入参为空；预期输出是可直接传给 spawn 的命令或绝对路径。
 * 边界异常：Windows 自动安装失败时返回 null；非 Windows 仍交给系统 PATH，保留 brew 等平台原生安装体验。
 */
export async function ensureLiveKitServerExecutable() {
  // 用户显式指定路径时必须尊重该路径；业务意图是避免配置错误时悄悄 fallback 到其他版本，导致排查困难。
  if (process.env.LIVEKIT_SERVER_PATH?.trim()) {
    return await resolveFromExplicitPath();
  }

  if (process.platform !== "win32") {
    return LIVEKIT_SERVER_COMMAND;
  }

  const pathExecutable = await resolveFromWindowsPath();

  if (pathExecutable) {
    return pathExecutable;
  }

  const cachedExecutable = await resolveFromWindowsCache();

  if (cachedExecutable) {
    return cachedExecutable;
  }

  if (process.env.LIVEKIT_AUTO_INSTALL === "0") {
    console.error("[livekit:dev] 未找到可用 livekit-server，且 LIVEKIT_AUTO_INSTALL=0 已禁用自动下载。");
    return null;
  }

  try {
    return await downloadWindowsLiveKitServer();
  } catch (error) {
    console.error(`[livekit:dev] 自动下载 LiveKit Server 失败：${error.message}`);
    console.error("[livekit:dev] 你也可以手动设置 LIVEKIT_SERVER_PATH 指向 livekit-server.exe。");
    return null;
  }
}
