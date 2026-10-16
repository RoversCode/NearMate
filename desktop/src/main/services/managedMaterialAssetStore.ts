import { randomUUID } from "node:crypto";
import { lstatSync, mkdirSync, realpathSync, rmSync, unlinkSync } from "node:fs";
import { lstat, mkdir, rm, unlink } from "node:fs/promises";
import { isAbsolute, relative, resolve } from "node:path";

type ManagedMaterialAssetStoreOptions = {
  rootDir: string;
  idGenerator?: () => string;
  logger?: Pick<Console, "warn">;
};

type ManagedAssetRegistration = {
  rootPath: string;
  assetPath: string;
};

export type ManagedMaterialAssetStore = {
  readonly rootDir: string;
  prepareRuntime(): Promise<void>;
  registerGeneratedAsset(path: string): string;
  release(managedAssetId: string): Promise<boolean>;
  releaseAll(): Promise<void>;
  releaseAllSync(): void;
};

/**
 * 创建项目生成材料副本的唯一登记与删除边界。
 *
 * @param options 专用临时目录与可选编号生成器。
 * @returns 只能凭已登记资产编号执行删除的运行期仓储。
 */
export function createManagedMaterialAssetStore(
  options: ManagedMaterialAssetStoreOptions
): ManagedMaterialAssetStore {
  const rootDir = resolve(options.rootDir);
  const idGenerator = options.idGenerator ?? randomUUID;
  const logger = options.logger ?? console;
  const assets = new Map<string, ManagedAssetRegistration>();
  const assetIdsByPath = new Map<string, string>();

  return {
    rootDir,

    async prepareRuntime(): Promise<void> {
      assets.clear();
      assetIdsByPath.clear();
      await resetRootDirectory(rootDir);
    },

    registerGeneratedAsset(path): string {
      const resolvedPath = resolve(path);
      const verifiedRoot = realpathSync(rootDir);
      const verifiedPath = realpathSync(resolvedPath);
      if (!isPathInside(verifiedRoot, verifiedPath)) {
        throw new Error("受管资产必须位于项目专用临时目录内");
      }

      const existingAssetId = assetIdsByPath.get(verifiedPath);
      if (existingAssetId) {
        return existingAssetId;
      }

      const managedAssetId = idGenerator();
      if (!managedAssetId.trim() || assets.has(managedAssetId)) {
        throw new Error("受管资产编号必须非空且在当前进程内唯一");
      }
      assets.set(managedAssetId, {
        rootPath: verifiedRoot,
        assetPath: verifiedPath
      });
      assetIdsByPath.set(verifiedPath, managedAssetId);
      return managedAssetId;
    },

    async release(managedAssetId): Promise<boolean> {
      const registration = assets.get(managedAssetId);
      if (!registration) {
        return false;
      }

      assets.delete(managedAssetId);
      assetIdsByPath.delete(registration.assetPath);
      try {
        const verifiedRoot = realpathSync(rootDir);
        const verifiedPath = realpathSync(registration.assetPath);
        if (
          verifiedRoot !== registration.rootPath
          || !isPathInside(registration.rootPath, verifiedPath)
        ) {
          logger.warn("[companion-main] 受管材料资产释放被拒绝：真实路径已变化", managedAssetId);
          return false;
        }

        await rm(verifiedPath, { recursive: true, force: true });
        return true;
      } catch (error) {
        logger.warn("[companion-main] 受管材料资产释放失败", managedAssetId, error);
        return false;
      }
    },

    async releaseAll(): Promise<void> {
      assets.clear();
      assetIdsByPath.clear();
      await resetRootDirectory(rootDir);
    },

    releaseAllSync(): void {
      assets.clear();
      assetIdsByPath.clear();
      resetRootDirectorySync(rootDir);
    }
  };
}

/**
 * 判断真实路径是否严格位于专用临时根目录内。
 *
 * @param rootDir 已解析符号链接的专用根目录。
 * @param candidatePath 已解析符号链接的候选资产路径。
 * @returns 候选路径位于根目录内部时返回 true。
 */
function isPathInside(rootDir: string, candidatePath: string): boolean {
  const relativePath = relative(rootDir, candidatePath);
  return Boolean(relativePath) && !relativePath.startsWith("..") && !isAbsolute(relativePath);
}

/**
 * 安全重建受管资产专用根目录。
 *
 * @param rootDir 项目专用临时根目录。
 * @returns 目录清理和重建完成后的 Promise。
 */
async function resetRootDirectory(rootDir: string): Promise<void> {
  try {
    const stats = await lstat(rootDir);
    if (stats.isSymbolicLink()) {
      await unlink(rootDir);
    } else {
      await rm(rootDir, { recursive: true, force: true });
    }
  } catch (error) {
    if (!isMissingPathError(error)) {
      throw error;
    }
  }
  await mkdir(rootDir, { recursive: true });
}

/**
 * 在 Electron 退出钩子中同步安全重建受管资产根目录。
 *
 * @param rootDir 项目专用临时根目录。
 * @returns 无返回值。
 */
function resetRootDirectorySync(rootDir: string): void {
  try {
    const stats = lstatSync(rootDir);
    if (stats.isSymbolicLink()) {
      unlinkSync(rootDir);
    } else {
      rmSync(rootDir, { recursive: true, force: true });
    }
  } catch (error) {
    if (!isMissingPathError(error)) {
      throw error;
    }
  }
  mkdirSync(rootDir, { recursive: true });
}

/**
 * 判断文件系统异常是否表示目标路径不存在。
 *
 * @param error 文件系统操作抛出的未知异常。
 * @returns 错误码为 ENOENT 时返回 true。
 */
function isMissingPathError(error: unknown): boolean {
  return (error as NodeJS.ErrnoException | null)?.code === "ENOENT";
}
