import type { Live2dModelCatalogEntry } from "./live2dModelTypes";

/**
 * DEFAULT_LIVE2D_MODEL_PATH 指向当前项目内复制好的默认 Live2D 资源。
 * 这里使用 renderer 公共资源相对路径，目的是让开发态与构建态都通过同一条 URL 访问模型文件。
 */
export const DEFAULT_LIVE2D_MODEL_PATH = "./live2d/resources/Haru/Haru.model3.json";

/**
 * resolveLive2dModelPath 负责统一模型路径覆盖规则。
 * 核心入参是可选的环境变量字符串，输出为最终可交给 renderer 的模型 URL。
 * 边界异常：空字符串或纯空白字符串都会被视为“未配置”，并回退到项目内默认资源。
 */
export function resolveLive2dModelPath(input?: string): string {
  const normalizedPath = input?.trim();

  if (normalizedPath) {
    return normalizedPath;
  }

  return DEFAULT_LIVE2D_MODEL_PATH;
}

/**
 * resolveSelectedLive2dModelPath 负责把“当前选中的模型目录名”解析成真实模型路径。
 * 核心入参是本地发现得到的 catalog、可选 selected_live2d_model_id，以及调用方显式给出的回退模型路径。
 * 边界：如果 selected id 为空、纯空白、或在 catalog 中找不到，就一律回退到调用方提供的 fallbackModelPath，避免 helper 把默认回退策略写死。
 */
export function resolveSelectedLive2dModelPath({
  catalog,
  selectedModelId,
  fallbackModelPath
}: {
  catalog: readonly Live2dModelCatalogEntry[];
  selectedModelId?: string;
  fallbackModelPath: string;
}): string {
  const normalizedSelectedModelId = selectedModelId?.trim();

  if (!normalizedSelectedModelId) {
    return fallbackModelPath;
  }

  const matchedCatalogEntry = catalog.find(
    (catalogEntry) => catalogEntry.id === normalizedSelectedModelId
  );

  if (!matchedCatalogEntry) {
    return fallbackModelPath;
  }

  return matchedCatalogEntry.modelPath;
}

/**
 * isSelectedLive2dModelAvailable 负责判断“当前设置里记住的模型目录名”是否仍然存在于可用 catalog。
 * 核心入参是可选 selected id 与当前 catalog；输出为布尔值，便于 avatar/panel 在不改解析策略的前提下显式感知旧设置是否失效。
 */
export function isSelectedLive2dModelAvailable({
  catalog,
  selectedModelId
}: {
  catalog: readonly Live2dModelCatalogEntry[];
  selectedModelId?: string;
}): boolean {
  const normalizedSelectedModelId = selectedModelId?.trim();

  if (!normalizedSelectedModelId) {
    return false;
  }

  return catalog.some((catalogEntry) => catalogEntry.id === normalizedSelectedModelId);
}
