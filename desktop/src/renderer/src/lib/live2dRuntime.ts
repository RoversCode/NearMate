import { extensions } from "pixi.js";
import { loadScriptOnce } from "./loadScriptOnce";
import {
  BUNDLED_CUBISM_CORE_MAX_MOC_VERSION,
  createUnsupportedCubismMocVersionMessage,
  isCubismMocVersionSupported,
  readCubismMocVersionFromBuffer
} from "../../../shared/live2dModelSupport";

const CUBISM_CORE_SCRIPT_URL = "./live2d/runtime/live2dcubismcore.min.js";

type CubismLive2dEngine = typeof import("untitled-pixi-live2d-engine/cubism");

type CubismCoreGlobal = {
  Live2DCubismCore?: {
    Version?: {
      csmGetVersion?: () => number;
    };
  };
};

let runtimeReadyTask: Promise<CubismLive2dEngine> | null = null;
let pluginRegistered = false;
let cubismConfigured = false;

/**
 * UnsupportedLive2dModelVersionError 表示模型资源本身存在，但其 moc 版本超出当前 bundled core 支持上限。
 * 这样 avatar 层可以把“目录存在但不可加载”的错误单独识别出来，给用户更明确的失败原因。
 */
export class UnsupportedLive2dModelVersionError extends Error {
  readonly mocVersion: number;
  readonly maxSupportedMocVersion: number;

  constructor(
    mocVersion: number,
    maxSupportedMocVersion = BUNDLED_CUBISM_CORE_MAX_MOC_VERSION
  ) {
    super(
      createUnsupportedCubismMocVersionMessage(mocVersion, maxSupportedMocVersion)
    );
    this.name = "UnsupportedLive2dModelVersionError";
    this.mocVersion = mocVersion;
    this.maxSupportedMocVersion = maxSupportedMocVersion;
  }
}

/**
 * loadCubism5Live2dEngine 负责准备 Pixi 8 + Cubism 5 Live2D 渲染前置条件并返回引擎模块。
 * 核心入参为空，输出为已完成 runtime 初始化的 Live2D 引擎模块。
 * 边界异常：Cubism Core 脚本缺失或执行失败时会抛出错误，由舞台组件进入失败态并输出诊断日志。
 */
export function loadCubism5Live2dEngine(): Promise<CubismLive2dEngine> {
  /**
   * 多个窗口或 React 严格模式可能重复调用初始化。
   * 这里缓存 Promise，确保 runtime 脚本、插件注册与内存配置只执行一次。
   */
  if (runtimeReadyTask) {
    return runtimeReadyTask;
  }

  runtimeReadyTask = (async () => {
    await loadScriptOnce(CUBISM_CORE_SCRIPT_URL);
    await waitForCubismCoreReady();

    /**
     * cubism 入口在模块求值阶段会检查 window.Live2DCubismCore。
     * 因此必须等项目内 core 脚本完成注入后再动态导入，不能在文件顶部静态 import。
     */
    const engine = await import("untitled-pixi-live2d-engine/cubism");

    /**
     * untitled-pixi-live2d-engine 要求在创建 Pixi renderer 前注册 Live2DPlugin。
     * 这一步放在 runtime helper 中，避免组件层忘记注册导致黑屏或渲染管线缺失。
     */
    if (!pluginRegistered) {
      extensions.add(engine.Live2DPlugin);
      pluginRegistered = true;
    }

    /**
     * Haru 属于 Cubism 5 模型，默认 16MB 通常可用。
     * 这里提高到 32MB，给物理、表情和后续多窗口预览留出稳定余量。
     */
    if (!cubismConfigured) {
      engine.configureCubismSDK({ memorySizeMB: 32 });
      cubismConfigured = true;
    }

    return engine;
  })().catch(error => {
    runtimeReadyTask = null;
    throw error;
  });

  return runtimeReadyTask;
}

/**
 * assertLive2dModelSupported 负责在真正调用引擎前，先对 model3.json 引用的 moc3 做一次兼容性预检。
 * 核心入参是 renderer 可访问的 model3.json URL；输出为空，只有在确认不兼容时抛出明确错误。
 * 边界异常：model3.json、moc3 拉取失败或结构缺失时，不在这里提前拦截，交给下游引擎保留原始加载错误语义。
 */
export async function assertLive2dModelSupported(
  modelPath: string,
  options: {
    assetBaseUrl?: string | URL;
  } = {}
): Promise<void> {
  const modelDefinitionResponse = await fetch(modelPath);

  if (!modelDefinitionResponse.ok) {
    return;
  }

  const modelDefinition = (await modelDefinitionResponse.json()) as {
    FileReferences?: {
      Moc?: string;
    };
  };
  const mocRelativePath = modelDefinition.FileReferences?.Moc;

  if (!mocRelativePath) {
    return;
  }

  const mocUrl = new URL(
    mocRelativePath,
    resolveRendererAssetBaseUrl(modelPath, options.assetBaseUrl)
  ).toString();
  const mocResponse = await fetch(mocUrl);

  if (!mocResponse.ok) {
    return;
  }

  const mocVersion = readCubismMocVersionFromBuffer(await mocResponse.arrayBuffer());

  if (mocVersion === null) {
    return;
  }

  if (!isCubismMocVersionSupported(mocVersion, BUNDLED_CUBISM_CORE_MAX_MOC_VERSION)) {
    throw new UnsupportedLive2dModelVersionError(
      mocVersion,
      BUNDLED_CUBISM_CORE_MAX_MOC_VERSION
    );
  }
}

/**
 * waitForCubismCoreReady 负责等待 Cubism Core 从“脚本已加载”进入“wasm/runtime 可调用”状态。
 * 核心入参是可选的等待参数与全局对象，输出为 runtime 真正 ready 后的 Promise。
 * 边界异常：如果超时仍无法调用 csmGetVersion，会抛出明确错误，避免后续 Live2D 引擎在半初始化状态下静默失败。
 */
export function waitForCubismCoreReady({
  timeoutMs = 3000,
  intervalMs = 25,
  globalTarget = window as CubismCoreGlobal,
  schedule = window.setTimeout.bind(window)
}: {
  timeoutMs?: number;
  intervalMs?: number;
  globalTarget?: CubismCoreGlobal;
  schedule?: (callback: () => void, delayMs: number) => unknown;
} = {}): Promise<void> {
  const startedAt = Date.now();

  return new Promise((resolve, reject) => {
    /**
     * Cubism Core 来自 Emscripten 输出，script.onload 并不等价于内部 wasm/runtime 已完成。
     * 这里用官方暴露的版本查询函数作为可用性探针，比检查对象存在更可靠。
     */
    function probeCore(): void {
      try {
        const version = globalTarget.Live2DCubismCore?.Version?.csmGetVersion?.();

        if (typeof version === "number" && Number.isFinite(version) && version > 0) {
          resolve();
          return;
        }
      } catch {
        /**
         * csmGetVersion 在 runtime 未完成时可能抛错。
         * 这里不立即失败，而是继续轮询，直到超时再给出明确诊断。
         */
      }

      if (Date.now() - startedAt >= timeoutMs) {
        reject(new Error("Cubism Core runtime 初始化超时，无法确认 Live2DCubismCore 可用。"));
        return;
      }

      schedule(probeCore, intervalMs);
    }

    probeCore();
  });
}

/**
 * resolveRendererAssetBaseUrl 负责把 renderer 使用的相对公共资源路径提升为可参与 URL 解析的绝对基准。
 * 这样 model3.json 中的相对 moc3 路径才能稳定拼接，不受当前页面是 avatar.html 还是 panel.html 影响。
 */
function resolveRendererAssetBaseUrl(
  modelPath: string,
  assetBaseUrl?: string | URL
): URL {
  if (assetBaseUrl) {
    return new URL(modelPath, assetBaseUrl);
  }

  return new URL(modelPath, window.location.href);
}
