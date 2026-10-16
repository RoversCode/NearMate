import { dirname, join, win32 } from "node:path";
import type { NearMateRuntimeLayout } from "./runtimeLayout";
import type { LiveKitConnectionSettings } from "./liveKitConfiguration";

/**
 * 为安装版 Backend 生成受限环境，防止开发机路径和原生库污染发行运行时。
 *
 * Args:
 *   paths: 已验证的安装运行时路径。
 *   userDataDir: 当前用户的数据目录。
 *   controlUrl: Backend 控制面地址。
 *   instanceId: 本次 Backend 启动的随机身份。
 *   base: 只用于读取 Windows 基础目录和临时目录。
 *   liveKitConnection: 壳层已确认的媒体服务配置，优先于宿主环境。
 *
 * Returns:
 *   仅包含允许继承字段和 NearMate 运行字段的子进程环境。
 */
export function createPackagedBackendEnvironment(
  paths: NearMateRuntimeLayout,
  userDataDir: string,
  controlUrl: string,
  instanceId: string,
  base: NodeJS.ProcessEnv = process.env,
  liveKitConnection?: LiveKitConnectionSettings,
): NodeJS.ProcessEnv {
  const environment = createPackagedRuntimeEnvironment(paths, userDataDir, base);
  if (liveKitConnection) {
    // Windows 环境名不区分大小写，先去掉继承别名，避免新旧凭据同时传给 Python。
    for (const key of Object.keys(environment)) {
      if (["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"].includes(key.toUpperCase())) delete environment[key];
    }
    Object.assign(environment, {
      LIVEKIT_URL: liveKitConnection.url, LIVEKIT_API_KEY: liveKitConnection.apiKey, LIVEKIT_API_SECRET: liveKitConnection.apiSecret
    });
  }
  return {
    ...environment,
    COMPANION_CONTROL_HTTP_URL: controlUrl,
    NEARMATE_RUNTIME_INSTANCE: instanceId,
  };
}

/**
 * 为所有安装态受管 Python 子进程生成共同的最小环境。
 *
 * Args:
 *   paths: 已验证的安装运行时路径。
 *   userDataDir: 当前用户可写数据目录。
 *   base: 只用于读取 Windows 基础目录和临时目录。
 *
 * Returns:
 *   不携带开发机密钥、Python 路径或 CUDA 路径的受限环境。
 */
export function createPackagedRuntimeEnvironment(
  paths: NearMateRuntimeLayout,
  userDataDir: string,
  base: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  const allowedKeys = new Set([
    "systemroot",
    "windir",
    "userprofile",
    "appdata",
    "localappdata",
    "programdata",
    "programfiles",
    "programfiles(x86)",
    "temp",
    "tmp",
    "username",
    "userdomain",
    "allusersprofile",
    "homedrive",
    "homepath",
    "number_of_processors",
    "processor_architecture",
    // 完整外部部署仍可提供媒体配置；壳层启动内置服务时会用本次随机凭据覆盖这些字段。
    "livekit_url",
    "livekit_api_key",
    "livekit_api_secret",
  ]);
  const environment: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(base)) {
    if (allowedKeys.has(key.toLowerCase()) && value !== undefined) {
      environment[key] = value;
    }
  }

  const windowsRoot = Object.entries(base).find(
    ([key]) => key.toLowerCase() === "systemroot",
  )?.[1] ?? "C:\\Windows";
  // Windows 原生组件会展开 %SystemDrive%；漏掉它会把系统缓存写进工作目录。
  const systemDrive = Object.entries(base).find(
    ([key]) => key.toLowerCase() === "systemdrive",
  )?.[1] || win32.parse(windowsRoot).root.replace(/[\\/]+$/, "");
  const systemDirectory = join(windowsRoot, "System32");
  const runtimeRoot = paths.runtimeRoot;
  const resourcesRoot = paths.resourcesRoot;
  if (!paths.manifestPath) {
    throw new Error("安装态 RuntimeLayout 缺少 runtime-manifest.json 路径");
  }
  const pathEntries = [
    dirname(paths.pythonExe),
    paths.nativeRoot,
    join(runtimeRoot, "tools", "claude"),
    join(runtimeRoot, "tools", "cua"),
    join(runtimeRoot, "tools", "git", "bin"),
    // Skill 预检和后台子进程从同一私有 PATH 找到随包媒体工具，不要求用户另装 FFmpeg。
    join(runtimeRoot, "tools", "ffmpeg", "bin"),
    systemDirectory,
    windowsRoot,
    join(systemDirectory, "Wbem"),
    join(systemDirectory, "WindowsPowerShell", "v1.0"),
  ];

  return {
    ...environment,
    SystemDrive: systemDrive,
    PATH: pathEntries.join(";"),
    NEARMATE_PACKAGED: "1",
    NEARMATE_RESOURCE_ROOT: resourcesRoot,
    NEARMATE_DATA_ROOT: userDataDir,
    NEARMATE_RUNTIME_ROOT: runtimeRoot,
    NEARMATE_RUNTIME_MANIFEST: paths.manifestPath,
    NEARMATE_RUNTIME_PROFILE: paths.profile,
    STREAMVOX_HOME: join(userDataDir, "streamvox"),
    PYTHONUTF8: "1",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    CLAUDE_CODE_GIT_BASH_PATH: join(runtimeRoot, "tools", "git", "bin", "bash.exe"),
  };
}
