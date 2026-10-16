import type { MaterialAttachment, MaterialContextSnapshot, MaterialShelfItemView, MaterialShelfView } from "./materialTypes";

const MAX_DISPLAY_NAME_CHARS = 33;

/**
 * summarizeMaterialItem 把材料项转换成稳定、短小的中文展示文案。
 *
 * @param item 当前本轮材料上下文中的材料项。
 * @returns 适合资料栏展示的单行摘要。
 */
export function summarizeMaterialItem(item: MaterialAttachment): string {
  const displayName = trimDisplayName(item.display_name || "未命名材料");

  if (item.media_class === "audio") {
    return `音频 ${displayName}`;
  }

  if (item.media_class === "video") {
    return `视频 ${displayName}`;
  }

  if (item.kind === "dragged_file") {
    return `文件 ${displayName}`;
  }

  if (item.kind === "dragged_folder") {
    return `文件夹 ${displayName}`;
  }

  if (item.kind === "selected_web") {
    return `网页选区 ${displayName}`;
  }

  if (item.kind === "clipboard_image" || item.kind === "selected_image") {
    return `图片 ${displayName}`;
  }

  if (item.kind === "clipboard_text" || item.kind === "selected_text") {
    return `文本 ${displayName}`;
  }

  return displayName;
}

/**
 * buildMaterialShelfView 生成本轮材料栏需要的只读视图模型。
 *
 * @param pool 后端当前材料上下文快照。
 * @returns 可直接渲染的资料栏视图模型。
 */
export function buildMaterialShelfView(pool: MaterialContextSnapshot | null): MaterialShelfView {
  const items = pool?.turn_material_context.staged_materials ?? [];
  if (items.length === 0) {
    return {
      isVisible: false,
      title: "本轮材料",
      countText: "",
      items: [],
      snapshotText: null
    };
  }

  return {
    isVisible: true,
    title: "本轮材料",
    countText: `${items.length} 项`,
    items: items.map(toShelfItemView),
    snapshotText: null
  };
}

function trimDisplayName(value: string): string {
  const normalizedValue = value.trim();
  if (normalizedValue.length <= MAX_DISPLAY_NAME_CHARS) {
    return normalizedValue;
  }

  return `${normalizedValue.slice(0, MAX_DISPLAY_NAME_CHARS)}...`;
}

function toShelfItemView(item: MaterialAttachment & { thumbnailDataUrl?: string | null }): MaterialShelfItemView {
  return {
    materialId: item.material_id,
    kind: item.kind,
    mediaClass: item.media_class,
    displayName: trimDisplayName(item.display_name || "未命名材料"),
    path: item.path,
    thumbnailDataUrl: item.thumbnailDataUrl ?? null
  };
}
