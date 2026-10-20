import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, posix, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Live2dModelCatalog, Live2dModelCatalogEntry } from "../../shared/live2dModelTypes";
import {
  BUNDLED_CUBISM_CORE_MAX_MOC_VERSION,
  isCubismMocVersionSupported,
  readCubismMocVersionFromBuffer
} from "../../shared/live2dModelSupport";

/**
 * renderer 侧当前通过这个公共 URL 根路径访问 Live2D 模型资源。
 * 源码目录、构建目录与公共 URL 统一使用标准的 resources 命名。
 */
const LIVE2D_RENDERER_PUBLIC_ROOT = "live2d/resources";

/**
 * 这里显式恢复当前模块目录，保证模型目录解析在 main 与 preload 两种打包入口下都能拿到一致相对路径。
 */
const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * loadLive2dModelCatalog 负责从本地资源目录发现可切换模型列表。
 * 核心入参是可选资源根目录；未提供时会自动在开发态与构建态候选目录中选择第一个存在的路径。
 * 边界：目录不存在、目录为空、或某个模型目录存在歧义时，都不会抛错中断，而是只跳过无效项。
 */
export function loadLive2dModelCatalog({
  resourcesRoot = resolveLive2dResourcesRoot()
}: {
  resourcesRoot?: string | null;
} = {}): Live2dModelCatalog {
  if (!resourcesRoot || !existsSync(resourcesRoot)) {
    return [];
  }

  return readdirSync(resourcesRoot, { withFileTypes: true })
    .filter((directoryEntry) => directoryEntry.isDirectory())
    .map((directoryEntry) => buildCatalogEntry(resourcesRoot, directoryEntry.name))
    .filter((catalogEntry): catalogEntry is Live2dModelCatalogEntry => catalogEntry !== null)
    .sort((leftEntry, rightEntry) => leftEntry.id.localeCompare(rightEntry.id));
}

/**
 * resolveLive2dResourcesRoot 统一开发态与构建态的本地资源目录候选。
 * 这里优先命中源码 public 目录，构建产物存在时再命中 dist 目录，避免开发时读取不到用户刚搬迁的资源。
 */
function resolveLive2dResourcesRoot(): string | null {
  const candidateDirectoryPaths = [
    resolve(__dirname, "../../src/renderer/public/live2d/resources"),
    resolve(__dirname, "../../dist/renderer/live2d/resources")
  ];

  for (const candidateDirectoryPath of candidateDirectoryPaths) {
    if (existsSync(candidateDirectoryPath)) {
      return candidateDirectoryPath;
    }
  }

  return null;
}

/**
 * buildCatalogEntry 只负责处理单个模型目录。
 * 业务意图是把“主 model3.json 的选择规则”限制在这一处，避免后续 panel/avatar 两侧各自猜路径。
 */
function buildCatalogEntry(
  resourcesDirectoryPath: string,
  modelId: string
): Live2dModelCatalogEntry | null {
  const modelDirectoryPath = join(resourcesDirectoryPath, modelId);
  const primaryModelFileName = selectPrimaryModelFileName(modelDirectoryPath, modelId);

  if (!primaryModelFileName) {
    return null;
  }

  /**
   * 模型目录只要能定位到主 model3.json，就继续做一次 moc 兼容性过滤。
   * 业务意图是把“当前 core 根本无法加载”的人物提前挡在 catalog 外，避免用户反复点到必失败选项。
   */
  const modelDefinitionPath = join(modelDirectoryPath, primaryModelFileName);

  if (!isModelCompatibleWithBundledCore(modelDefinitionPath)) {
    return null;
  }

  return {
    id: modelId,
    label: modelId === "DeepSeekChibi" ? "大肥鱼（Q版）" : modelId,
    modelPath: `./${posix.join(LIVE2D_RENDERER_PUBLIC_ROOT, modelId, primaryModelFileName)}`
  };
}

/**
 * selectPrimaryModelFileName 统一主模型定义文件的挑选规则。
 * 规则顺序固定为：
 * 1. 优先选择与目录同名的 `目录名.model3.json`
 * 2. 如果没有同名文件，但目录里只有一个 `*.model3.json`，则回退使用该唯一候选
 * 3. 如果候选超过一个且没有同名主文件，则返回 null，避免把不稳定路径暴露给 UI
 */
function selectPrimaryModelFileName(modelDirectoryPath: string, modelId: string): string | null {
  const candidateModelFileNames = readdirSync(modelDirectoryPath, { withFileTypes: true })
    .filter((directoryEntry) => directoryEntry.isFile() && directoryEntry.name.endsWith(".model3.json"))
    .map((directoryEntry) => directoryEntry.name);

  const expectedPrimaryFileName = `${modelId}.model3.json`.toLowerCase();
  const sameNamePrimaryFileName = candidateModelFileNames.find(
    (candidateModelFileName) => candidateModelFileName.toLowerCase() === expectedPrimaryFileName
  );

  if (sameNamePrimaryFileName) {
    return sameNamePrimaryFileName;
  }

  if (candidateModelFileNames.length === 1) {
    return candidateModelFileNames[0];
  }

  return null;
}

/**
 * isModelCompatibleWithBundledCore 负责判断主模型定义引用的 moc3 是否落在当前 bundled core 支持范围内。
 * 核心入参是 model3.json 绝对路径；输出为布尔值。
 * 边界异常：如果 model3.json 缺少 FileReferences.Moc、JSON 非法或 moc3 读取失败，则这里返回 true，
 * 原因是目录扫描阶段不应因为解析异常误删潜在可用模型，真实加载失败仍由 avatar 运行时兜底。
 */
function isModelCompatibleWithBundledCore(modelDefinitionPath: string): boolean {
  try {
    const modelDefinition = JSON.parse(readFileSync(modelDefinitionPath, "utf-8")) as {
      FileReferences?: {
        Moc?: string;
      };
    };
    const mocRelativePath = modelDefinition.FileReferences?.Moc;

    if (!mocRelativePath) {
      return true;
    }

    const mocFilePath = join(dirname(modelDefinitionPath), mocRelativePath);

    if (!existsSync(mocFilePath)) {
      return true;
    }

    /**
     * moc3 头版本只需要前 8 个字节即可判断。
     * 这里直接读取全文件，保持实现扁平；资源目录扫描频率很低，这点 IO 成本可接受。
     */
    const mocVersion = readCubismMocVersionFromBuffer(readFileSync(mocFilePath));

    if (mocVersion === null) {
      return true;
    }

    return isCubismMocVersionSupported(
      mocVersion,
      BUNDLED_CUBISM_CORE_MAX_MOC_VERSION
    );
  } catch {
    return true;
  }
}
