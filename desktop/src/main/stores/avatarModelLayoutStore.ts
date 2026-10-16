import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

/**
 * AvatarModelWindowBounds 表示按人物保存的桌宠窗口几何。
 */
export type AvatarModelWindowBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarModelLayoutWorkArea 表示用于恢复布局时的显示器工作区。
 */
type AvatarModelLayoutWorkArea = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarModelLayoutStore 是桌面端按人物保存窗口几何的最小仓储接口。
 */
export type AvatarModelLayoutStore = {
  read(modelId: string, workArea: AvatarModelLayoutWorkArea): AvatarModelWindowBounds | null;
  save(modelId: string, bounds: AvatarModelWindowBounds): void;
  reset(modelId: string): void;
};

type AvatarModelLayoutFile = {
  version: number;
  layouts: Record<string, AvatarModelWindowBounds>;
};

const AVATAR_MODEL_LAYOUT_FILE_VERSION = 1;

/**
 * createAvatarModelLayoutStore 创建按 Live2D 人物保存窗口几何的本地仓储。
 *
 * @param filePath 存放布局 JSON 的绝对路径。
 * @returns 可读写、可重置单个人物布局的仓储对象。
 */
export function createAvatarModelLayoutStore(filePath: string): AvatarModelLayoutStore {
  function loadFile(): AvatarModelLayoutFile {
    if (!existsSync(filePath)) {
      return createEmptyLayoutFile();
    }

    try {
      const payload = JSON.parse(readFileSync(filePath, "utf-8"));
      return normalizeLayoutFile(payload);
    } catch {
      return createEmptyLayoutFile();
    }
  }

  function saveFile(snapshot: AvatarModelLayoutFile): void {
    mkdirSync(dirname(filePath), { recursive: true });
    writeFileSync(
      filePath,
      `${JSON.stringify(snapshot, null, 2)}\n`,
      "utf-8"
    );
  }

  return {
    read(modelId, workArea) {
      const normalizedModelId = normalizeModelId(modelId);

      if (!normalizedModelId) {
        return null;
      }

      const bounds = loadFile().layouts[normalizedModelId];

      if (!bounds) {
        return null;
      }

      return clampBoundsToWorkArea(bounds, workArea);
    },
    save(modelId, bounds) {
      const normalizedModelId = normalizeModelId(modelId);
      const normalizedBounds = normalizeBounds(bounds);

      if (!normalizedModelId || !normalizedBounds) {
        return;
      }

      const snapshot = loadFile();
      snapshot.layouts[normalizedModelId] = normalizedBounds;
      saveFile(snapshot);
    },
    reset(modelId) {
      const normalizedModelId = normalizeModelId(modelId);

      if (!normalizedModelId) {
        return;
      }

      const snapshot = loadFile();
      delete snapshot.layouts[normalizedModelId];
      saveFile(snapshot);
    }
  };
}

/**
 * createEmptyLayoutFile 生成当前版本的空布局文件结构。
 */
function createEmptyLayoutFile(): AvatarModelLayoutFile {
  return {
    version: AVATAR_MODEL_LAYOUT_FILE_VERSION,
    layouts: {}
  };
}

/**
 * normalizeLayoutFile 从未知 JSON 载荷中提取当前版本可用的布局记录。
 *
 * @param payload 从磁盘读取并解析出的未知 JSON。
 * @returns 当前仓储可消费的布局文件快照。
 */
function normalizeLayoutFile(payload: unknown): AvatarModelLayoutFile {
  if (!payload || typeof payload !== "object") {
    return createEmptyLayoutFile();
  }

  const candidate = payload as Partial<AvatarModelLayoutFile>;

  if (candidate.version !== AVATAR_MODEL_LAYOUT_FILE_VERSION || !candidate.layouts) {
    return createEmptyLayoutFile();
  }

  const layouts: Record<string, AvatarModelWindowBounds> = {};

  for (const [modelId, bounds] of Object.entries(candidate.layouts)) {
    const normalizedModelId = normalizeModelId(modelId);
    const normalizedBounds = normalizeBounds(bounds);

    if (!normalizedModelId || !normalizedBounds) {
      continue;
    }

    layouts[normalizedModelId] = normalizedBounds;
  }

  return {
    version: AVATAR_MODEL_LAYOUT_FILE_VERSION,
    layouts
  };
}

/**
 * normalizeBounds 校验并规整窗口几何。
 *
 * @param payload 外部输入的未知窗口几何。
 * @returns 合法窗口几何；非法时返回 null。
 */
function normalizeBounds(payload: unknown): AvatarModelWindowBounds | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<AvatarModelWindowBounds>;

  if (
    !isFiniteNumber(candidate.x) ||
    !isFiniteNumber(candidate.y) ||
    !isFiniteNumber(candidate.width) ||
    !isFiniteNumber(candidate.height) ||
    candidate.width <= 0 ||
    candidate.height <= 0
  ) {
    return null;
  }

  return {
    x: Math.round(candidate.x),
    y: Math.round(candidate.y),
    width: Math.round(candidate.width),
    height: Math.round(candidate.height)
  };
}

/**
 * clampBoundsToWorkArea 把保存过的窗口位置收回当前显示器工作区。
 *
 * @param bounds 已保存的窗口几何。
 * @param workArea 当前显示器工作区。
 * @returns 尺寸不变、位置可见的窗口几何。
 */
function clampBoundsToWorkArea(
  bounds: AvatarModelWindowBounds,
  workArea: AvatarModelLayoutWorkArea
): AvatarModelWindowBounds {
  const maxX = Math.max(workArea.x, workArea.x + workArea.width - bounds.width);
  const maxY = Math.max(workArea.y, workArea.y + workArea.height - bounds.height);

  return {
    x: clamp(bounds.x, workArea.x, maxX),
    y: clamp(bounds.y, workArea.y, maxY),
    width: bounds.width,
    height: bounds.height
  };
}

/**
 * normalizeModelId 规整人物模型标识。
 */
function normalizeModelId(modelId: string): string | null {
  const normalizedModelId = modelId.trim();
  return normalizedModelId.length > 0 ? normalizedModelId : null;
}

/**
 * isFiniteNumber 判断值是否为可持久化的有限数字。
 */
function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * clamp 把数值限制在闭区间内。
 */
function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
