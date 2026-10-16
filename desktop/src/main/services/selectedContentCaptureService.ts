import { statSync } from "node:fs";
import type { ManagedMaterialAssetStore } from "./managedMaterialAssetStore";
import type { MaterialItemPayload } from "./materialsClient";
import {
  classifyCapturedMaterial
} from "./capturedMaterialClassifier";
import {
  writeCapturedMaterial,
  type SelectedContentFileStore
} from "./managedMaterialWriter";
import {
  captureSelectionClipboardTransaction,
  type SelectedContentClipboardPort,
  type WindowsSelectionCapturePort
} from "./selectionClipboardTransaction";

type SelectedContentCaptureServiceOptions = {
  platform: NodeJS.Platform;
  attachmentDir: string;
  managedAssetStore: Pick<ManagedMaterialAssetStore, "registerGeneratedAsset">;
  clipboard: SelectedContentClipboardPort;
  selectionCapture: WindowsSelectionCapturePort;
  fileStore: SelectedContentFileStore;
  pathInspector?: { isDirectory(path: string): boolean };
  clock?: { now(): Date };
  idGenerator?: { nextId(): string };
  logger?: Pick<Console, "info">;
};

export type SelectedContentCaptureService = {
  captureSelectedContent(): Promise<MaterialItemPayload[]>;
};

/**
 * 组合剪贴板事务、纯分类和受管资产写入三个独立阶段。
 *
 * @param options 系统选区端口和受管材料写入依赖。
 * @returns 只暴露一次完整选区采集动作的服务。
 */
export function createSelectedContentCaptureService(
  options: SelectedContentCaptureServiceOptions
): SelectedContentCaptureService {
  return {
    async captureSelectedContent() {
      const raw = await captureSelectionClipboardTransaction(
        options.platform,
        options.clipboard,
        options.selectionCapture
      );
      const pathInspector = options.pathInspector ?? { isDirectory: isDirectoryPath };
      const directoryPaths = new Set(raw.filePaths.flatMap(path => {
        const normalized = path.trim();
        return normalized && pathInspector.isDirectory(normalized) ? [normalized] : [];
      }));
      const classification = classifyCapturedMaterial(raw, directoryPaths);
      const { captured, diagnostics } = classification;
      options.logger?.info("[companion-main] selection capture completed", {
        resultKind: captured.kind,
        itemCount: captured.kind === "paths" ? captured.items.length : captured.kind === "empty" ? 0 : 1,
        clipboardChanged: raw.clipboardChanged,
        elapsedMs: raw.elapsedMs,
        timedOut: !raw.clipboardChanged,
        standaloneHttpUrl: diagnostics.standaloneHttpUrl,
        linkCount: diagnostics.linkCount,
        imageCount: diagnostics.imageCount,
        fallbackReason: diagnostics.fallbackReason
      });
      return await writeCapturedMaterial(captured, {
        attachmentDir: options.attachmentDir,
        managedAssetStore: options.managedAssetStore,
        fileStore: options.fileStore,
        clock: options.clock,
        idGenerator: options.idGenerator
      });
    }
  };
}

/**
 * 判断本地路径是否为目录，失效路径交给 Backend 统一拒绝。
 *
 * @param path 系统选区返回的本地路径。
 * @returns 路径当前可确认为目录时返回 true。
 */
function isDirectoryPath(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}
