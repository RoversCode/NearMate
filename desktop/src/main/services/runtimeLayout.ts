import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

type NearMateRuntimeProfile = "cuda" | "universal";

type RuntimeManifestPaths = {
  python: string;
  backend: string;
  native: string;
  shared: string;
  console: string;
  aec_package: string;
  model_catalog: string;
  seed_character_cards: string;
  assets: string;
  livekit_server?: string;
};

export type RuntimeManifestFileRecord = {
  path: string;
  size: number;
  sha256: string;
};

export type RuntimeManifest = {
  schema_version: 3;
  product: "NearMate";
  platform: "windows-x64";
  python_version: "3.12.13";
  profile: NearMateRuntimeProfile;
  paths: RuntimeManifestPaths;
  required_files: string[];
  files: RuntimeManifestFileRecord[];
};

export type NearMateRuntimeLayout = {
  resourcesRoot: string;
  runtimeRoot: string;
  manifestPath: string | null;
  manifest: RuntimeManifest | null;
  profile: NearMateRuntimeProfile;
  pythonExe: string;
  backendCwd: string;
  nativeRoot: string;
  sharedRoot: string;
  sidecarPackageRoot: string;
  consoleDistDir: string;
  modelCatalogPath: string;
  seedCharacterCardRoot: string;
  assetsRoot: string;
  liveKitServerExe: string | null;
};

const VALID_PROFILES = new Set<NearMateRuntimeProfile>(["cuda", "universal"]);

/**
 * 读取并验证安装包的唯一 RuntimeLayout 清单。
 *
 * Args:
 *   manifestPath: runtime 目录中的清单绝对路径。
 *
 * Returns:
 *   已验证的运行时清单。
 *
 * Raises:
 *   Error: 清单缺失、版本不匹配、路径越界或字段非法时抛出。
 */
export function readRuntimeManifest(manifestPath: string): RuntimeManifest {
  if (!existsSync(manifestPath)) {
    throw new Error(`安装包 runtime-manifest.json 缺失：${manifestPath}`);
  }

  let payload: unknown;
  try {
    payload = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (error) {
    throw new Error(`安装包 runtime-manifest.json 无法读取：${manifestPath}`, { cause: error });
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error(`安装包 runtime-manifest.json 格式无效：${manifestPath}`);
  }
  const candidate = payload as Partial<RuntimeManifest>;
  if (candidate.schema_version !== 3 || candidate.product !== "NearMate" ||
      candidate.platform !== "windows-x64" || candidate.python_version !== "3.12.13" ||
      !VALID_PROFILES.has(candidate.profile as NearMateRuntimeProfile)) {
    throw new Error(`安装包 runtime-manifest.json 基础字段无效：${manifestPath}`);
  }

  const paths = candidate.paths;
  if (!paths || typeof paths !== "object" || Array.isArray(paths)) {
    throw new Error(`安装包 runtime-manifest.json 缺少 paths：${manifestPath}`);
  }
  const pathKeys: Array<keyof RuntimeManifestPaths> = [
    "python", "backend", "native", "shared", "console", "aec_package",
    "model_catalog", "seed_character_cards", "assets"
  ];
  for (const key of pathKeys) {
    const value = paths[key];
    if (typeof value !== "string") {
      throw new Error(`安装包 runtime-manifest.json 缺少路径字段 ${key}：${manifestPath}`);
    }
    validateRelativeRuntimePath(value, key);
  }
  if (!Array.isArray(candidate.required_files) || candidate.required_files.length === 0) {
    throw new Error(`安装包 runtime-manifest.json 缺少 required_files：${manifestPath}`);
  }
  for (const filePath of candidate.required_files) {
    if (typeof filePath !== "string") {
      throw new Error(`安装包 runtime-manifest.json 的 required_files 含非法值：${manifestPath}`);
    }
    validateRelativeRuntimePath(filePath, "required_files");
  }
  // 旧清单可继续用于完整外部部署；新内置程序必须进入启动时的摘要校验名单。
  if (paths.livekit_server !== undefined) {
    if (typeof paths.livekit_server !== "string") throw new Error("安装包 livekit_server 路径无效");
    validateRelativeRuntimePath(paths.livekit_server, "livekit_server");
    if (!candidate.required_files.includes(paths.livekit_server)) throw new Error("安装包 livekit_server 必须登记为 required_files");
  }

  if (!Array.isArray(candidate.files) || candidate.files.length === 0) {
    throw new Error(`安装包 runtime-manifest.json 缺少非空 files：${manifestPath}`);
  }
  const seenFiles = new Set<string>();
  const fileRecords = candidate.files as unknown[];
  for (const record of fileRecords) {
    if (!record || typeof record !== "object" || Array.isArray(record)) {
      throw new Error(`安装包 runtime-manifest.json 的 files 含非法记录：${manifestPath}`);
    }
    const fileRecord = record as { path?: unknown; size?: unknown; sha256?: unknown };
    if (
      typeof fileRecord.path !== "string" ||
      !Number.isSafeInteger(fileRecord.size) ||
      (fileRecord.size as number) < 0 ||
      typeof fileRecord.sha256 !== "string" ||
      !/^[0-9a-f]{64}$/i.test(fileRecord.sha256)
    ) {
      throw new Error(`安装包 runtime-manifest.json 的 files 记录无效：${manifestPath}`);
    }
    validateRelativeRuntimePath(fileRecord.path, "files");
    const canonicalPath = fileRecord.path.toLowerCase();
    if (seenFiles.has(canonicalPath)) {
      throw new Error(`安装包 runtime-manifest.json 的 files 存在重复路径：${fileRecord.path}`);
    }
    seenFiles.add(canonicalPath);
  }
  for (const requiredFile of candidate.required_files) {
    if (!seenFiles.has(requiredFile.toLowerCase())) {
      throw new Error(`安装包 runtime-manifest.json 的 required_files 未登记摘要：${requiredFile}`);
    }
  }

  return candidate as RuntimeManifest;
}

/**
 * 解析开发态或安装态的统一运行时布局。
 *
 * Args:
 *   input: Electron 打包状态、资源目录、开发仓库根目录和 profile。
 *
 * Returns:
 *   Backend、AEC、Console 和资源校验共同使用的绝对路径快照。
 */
export function resolveNearMateRuntimeLayout(input: {
  isPackaged: boolean;
  resourcesPath?: string;
  repoRoot?: string;
  profile?: string | null;
}): NearMateRuntimeLayout {
  const resourcesRoot = resolve(input.isPackaged ? input.resourcesPath ?? "" : input.repoRoot ?? process.cwd());
  const runtimeRoot = input.isPackaged ? join(resourcesRoot, "runtime") : resourcesRoot;
  const manifestPath = input.isPackaged ? join(runtimeRoot, "runtime-manifest.json") : null;
  const manifest = manifestPath ? readRuntimeManifest(manifestPath) : null;
  const profile = manifest?.profile ?? normalizeRuntimeProfile(input.profile ?? process.env["NEARMATE_RUNTIME_PROFILE"]);
  const paths = manifest?.paths ?? developmentPaths();

  return {
    resourcesRoot,
    runtimeRoot,
    manifestPath,
    manifest,
    profile,
    pythonExe: join(runtimeRoot, paths.python),
    backendCwd: join(runtimeRoot, paths.backend),
    nativeRoot: join(runtimeRoot, paths.native),
    sharedRoot: join(runtimeRoot, paths.shared),
    sidecarPackageRoot: join(runtimeRoot, paths.aec_package),
    consoleDistDir: join(runtimeRoot, paths.console),
    modelCatalogPath: join(runtimeRoot, paths.model_catalog),
    seedCharacterCardRoot: join(runtimeRoot, paths.seed_character_cards),
    assetsRoot: join(runtimeRoot, paths.assets),
    liveKitServerExe: paths.livekit_server ? join(runtimeRoot, paths.livekit_server) : null
  };
}

/**
 * 校验 profile 输入，避免未知值悄悄落到错误的原生后端。
 *
 * Args:
 *   value: 环境变量或开发启动参数。
 *
 * Returns:
 *   规范化的 cuda 或 universal。
 */
function normalizeRuntimeProfile(value: string | null | undefined): NearMateRuntimeProfile {
  const normalized = (value ?? "cuda").trim().toLowerCase() as NearMateRuntimeProfile;
  if (!VALID_PROFILES.has(normalized)) {
    throw new Error("NEARMATE_RUNTIME_PROFILE 只能是 cuda 或 universal");
  }
  return normalized;
}

function developmentPaths(): RuntimeManifestPaths {
  return {
    python: ".venv/Scripts/python.exe",
    backend: ".",
    native: "backend/bin",
    shared: "shared",
    console: "console/dist",
    aec_package: "tools/audio-aec-poc/src/audio_aec_sidecar",
    model_catalog: "dist/installers/manifests/model-catalog.json",
    seed_character_cards: "backend/data/character-cards",
    assets: "assets"
  };
}

function validateRelativeRuntimePath(value: string, field: string): void {
  const segments = value.split("/");
  const hasControlCharacter = [...value].some(character => {
    const code = character.charCodeAt(0);
    return code < 0x20 || code === 0x7f;
  });
  const hasDrivePrefix = /^[A-Za-z]:/.test(value);
  const hasWindowsReservedName = segments.some(segment => {
    const baseName = segment.split(".", 1)[0].toUpperCase();
    return /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/.test(baseName);
  });
  if (
    !value ||
    value.startsWith("/") ||
    value.startsWith("//") ||
    hasDrivePrefix ||
    value.includes("\\") ||
    hasControlCharacter ||
    segments.some(segment => segment.length === 0 || segment === "." || segment === "..") ||
    segments.some(segment => segment.endsWith(" ") || segment.endsWith(".")) ||
    hasWindowsReservedName
  ) {
    throw new Error(`RuntimeLayout 路径 ${field} 必须是正斜杠相对路径：${value}`);
  }
}
