import type { ChatComposerAttachment } from "./ChatComposer";
import type { MaterialContextSnapshot } from "../materials/materialTypes";

const MATERIAL_KINDS = new Set<ChatComposerAttachment["kind"]>([
  "clipboard_text",
  "clipboard_image",
  "dragged_file",
  "dragged_folder",
  "selected_text",
  "selected_image",
  "selected_web"
]);

/**
 * materialItemsFromFiles 把拖入的本地文件转换为聊天材料。
 *
 * Args:
 *   files: 浏览器拖拽事件中的文件列表。
 *   resolvePath: 从 File 解析本地路径的 preload 能力。
 *
 * Returns:
 *   已过滤空路径的材料数组。
 */
export function materialItemsFromFiles(
  files: FileList | File[],
  resolvePath: (file: File) => string
): ChatComposerAttachment[] {
  return Array.from(files)
    .map<ChatComposerAttachment | null>(file => {
      const path = resolvePath(file).trim();
      if (!path) {
        return null;
      }

      return {
        kind: "dragged_file",
        source: "drag_drop",
        display_name: file.name || path,
        path
      };
    })
    .filter((item): item is ChatComposerAttachment => item !== null);
}

/**
 * normalizeCapturedMaterial 校验主进程返回的路径材料。
 *
 * Args:
 *   value: IPC 返回值。
 *
 * Returns:
 *   合法材料或 null。
 */
export function normalizeCapturedMaterial(value: unknown): ChatComposerAttachment | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }

  const candidate = value as Record<string, unknown>;
  if (typeof candidate.kind !== "string" || !MATERIAL_KINDS.has(candidate.kind as ChatComposerAttachment["kind"])) {
    return null;
  }

  if (typeof candidate.source !== "string" || typeof candidate.display_name !== "string") {
    return null;
  }

  const path = typeof candidate.path === "string" ? candidate.path.trim() : "";
  if (!path) {
    return null;
  }
  return {
    kind: candidate.kind as ChatComposerAttachment["kind"],
    source: candidate.source,
    display_name: candidate.display_name,
    path,
    ...(typeof candidate.managed_asset_id === "string" && candidate.managed_asset_id.trim()
      ? { managed_asset_id: candidate.managed_asset_id.trim() }
      : {})
  };
}

/**
 * 确认受管资产已经出现在 Backend 返回的本轮 staged 权威快照中。
 *
 * Args:
 *   context: staged 写入命令返回的材料上下文。
 *   managedAssetId: 主进程为本地副本分配的受管资产编号。
 *
 * Returns:
 *   权威 staged 材料中存在同一编号时返回真。
 */
export function hasStagedManagedAsset(
  context: MaterialContextSnapshot,
  managedAssetId: string
): boolean {
  return context.turn_material_context.staged_materials.some(
    item => item.managed_asset_id === managedAssetId
  );
}
