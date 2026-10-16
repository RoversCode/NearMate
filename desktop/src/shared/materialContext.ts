export type MaterialKind =
  | "clipboard_text"
  | "clipboard_image"
  | "dragged_file"
  | "dragged_folder"
  | "selected_text"
  | "selected_image"
  | "selected_web";

const MATERIAL_KINDS = new Set<MaterialKind>([
  "clipboard_text", "clipboard_image", "dragged_file", "dragged_folder",
  "selected_text", "selected_image", "selected_web"
]);

type MaterialNodeKind = "file" | "directory";
type MaterialMediaClass = "text" | "mixed_or_document" | "audio" | "video";
export type RecentMaterialStatus = "available" | "missing" | "type_changed" | "unsafe" | "unreadable";

const MATERIAL_NODE_KINDS = new Set<MaterialNodeKind>(["file", "directory"]);
const MATERIAL_MEDIA_CLASSES = new Set<MaterialMediaClass>([
  "text",
  "mixed_or_document",
  "audio",
  "video"
]);
const RECENT_MATERIAL_STATUSES = new Set<RecentMaterialStatus>([
  "available",
  "missing",
  "type_changed",
  "unsafe",
  "unreadable"
]);

export type MaterialAttachment = {
  material_id: string;
  turn_id: string;
  kind: MaterialKind;
  source: string;
  display_name: string;
  path: string;
  node_kind: MaterialNodeKind;
  created_at: string;
  media_class: MaterialMediaClass;
  managed_asset_id?: string | null;
};

export type RecentMaterial = {
  display_name: string;
  path: string;
  material_kind: MaterialKind;
  node_kind: MaterialNodeKind;
  media_class: MaterialMediaClass;
  status: RecentMaterialStatus;
};

export type MaterialContextSnapshot = {
  revision: number;
  recent_materials: RecentMaterial[];
  turn_material_context: {
    staged_materials: MaterialAttachment[];
    active: {
      context_id: string;
      turn_id: string;
      created_at: string;
      attachments: MaterialAttachment[];
    } | null;
  };
};

/**
 * 读取 Backend 权威材料快照。
 *
 * @param value 尚未验证的 HTTP 或 WebSocket 载荷。
 * @returns 结构完整且带非负 revision 的材料快照。
 */
export function readMaterialContextSnapshot(value: unknown): MaterialContextSnapshot {
  const candidate = readObject(value, "material context snapshot");
  assertExactKeys(
    candidate,
    ["revision", "turn_material_context", "recent_materials"],
    "material context snapshot"
  );
  if (!Number.isInteger(candidate.revision) || (candidate.revision as number) < 0) {
    throw new Error("material context snapshot revision is invalid");
  }
  const turnContext = readObject(candidate.turn_material_context, "turn material context");
  assertExactKeys(
    turnContext,
    ["staged_materials", "active"],
    "turn material context"
  );
  const stagedMaterials = readAttachmentList(turnContext.staged_materials);
  const active = turnContext.active === null
    ? null
    : readActiveContext(turnContext.active);
  return {
    revision: candidate.revision as number,
    recent_materials: readRecentMaterials(candidate.recent_materials),
    turn_material_context: {
      staged_materials: stagedMaterials,
      active
    }
  };
}

/**
 * 只接受更高 revision，屏蔽重复响应、旧广播和乱序网络包。
 *
 * @param current 当前已经展示的快照。
 * @param incoming 新收到的权威快照。
 * @returns revision 更新后的唯一投影。
 */
export function reduceMaterialContextSnapshot(
  current: MaterialContextSnapshot | null,
  incoming: MaterialContextSnapshot
): MaterialContextSnapshot {
  if (current === null || incoming.revision > current.revision) {
    return incoming;
  }
  return current;
}

function readAttachmentList(value: unknown): MaterialAttachment[] {
  if (!Array.isArray(value)) {
    throw new Error("material attachment list is invalid");
  }
  return value.map(readAttachment);
}

function readAttachment(value: unknown): MaterialAttachment {
  const item = readObject(value, "material attachment");
  assertOnlyKeys(item, [
    "material_id",
    "turn_id",
    "kind",
    "source",
    "display_name",
    "path",
    "node_kind",
    "created_at",
    "media_class",
    "managed_asset_id"
  ], "material attachment");
  assertRequiredKeys(item, [
    "material_id",
    "turn_id",
    "kind",
    "source",
    "display_name",
    "path",
    "node_kind",
    "created_at",
    "media_class"
  ], "material attachment");
  const kind = readString(item.kind, "material kind") as MaterialKind;
  if (!MATERIAL_KINDS.has(kind)) {
    throw new Error("material kind is invalid");
  }
  const nodeKind = readMaterialNodeKind(item.node_kind);
  const mediaClass = readMaterialMediaClass(item.media_class);
  return {
    material_id: readString(item.material_id, "material id"),
    turn_id: readString(item.turn_id, "material turn id"),
    kind,
    source: readString(item.source, "material source"),
    display_name: readString(item.display_name, "material display name"),
    path: readString(item.path, "material path"),
    node_kind: nodeKind,
    created_at: readString(item.created_at, "material created time"),
    media_class: mediaClass,
    managed_asset_id: readOptionalString(item.managed_asset_id)
  };
}

/**
 * 读取 Backend 返回的近期材料列表，并严格校验每项字段。
 *
 * @param value 尚未验证的近期材料数组。
 * @returns 可以安全进入 Desktop 状态的近期材料列表。
 */
function readRecentMaterials(value: unknown): RecentMaterial[] {
  if (!Array.isArray(value)) {
    throw new Error("recent material list is invalid");
  }
  return value.map(item => {
    const candidate = readObject(item, "recent material");
    assertExactKeys(
      candidate,
      ["display_name", "path", "material_kind", "node_kind", "media_class", "status"],
      "recent material"
    );
    const materialKind = readString(candidate.material_kind, "recent material kind") as MaterialKind;
    if (!MATERIAL_KINDS.has(materialKind)) {
      throw new Error("recent material kind is invalid");
    }
    const status = readString(candidate.status, "recent material status") as RecentMaterialStatus;
    if (!RECENT_MATERIAL_STATUSES.has(status)) {
      throw new Error("recent material status is invalid");
    }
    return {
      display_name: readString(candidate.display_name, "recent material display name"),
      path: readString(candidate.path, "recent material path"),
      material_kind: materialKind,
      node_kind: readMaterialNodeKind(candidate.node_kind),
      media_class: readMaterialMediaClass(candidate.media_class),
      status
    };
  });
}

/**
 * 读取材料的真实文件系统节点类型。
 *
 * @param value 尚未验证的节点类型字段。
 * @returns 文件或目录这两种正式节点类型之一。
 */
function readMaterialNodeKind(value: unknown): MaterialNodeKind {
  const nodeKind = readString(value, "material node kind") as MaterialNodeKind;
  if (!MATERIAL_NODE_KINDS.has(nodeKind)) {
    throw new Error("material node kind is invalid");
  }
  return nodeKind;
}

/**
 * 读取材料接纳阶段确定的媒体大类。
 *
 * @param value 尚未验证的媒体类型字段。
 * @returns Desktop 契约支持的正式媒体大类。
 */
function readMaterialMediaClass(value: unknown): MaterialMediaClass {
  const mediaClass = readString(value, "material media class") as MaterialMediaClass;
  if (!MATERIAL_MEDIA_CLASSES.has(mediaClass)) {
    throw new Error("material media class is invalid");
  }
  return mediaClass;
}

function readActiveContext(value: unknown): MaterialContextSnapshot["turn_material_context"]["active"] {
  const item = readObject(value, "active material context");
  assertExactKeys(
    item,
    ["context_id", "turn_id", "created_at", "attachments"],
    "active material context"
  );
  return {
    context_id: readString(item.context_id, "active context id"),
    turn_id: readString(item.turn_id, "active turn id"),
    created_at: readString(item.created_at, "active created time"),
    attachments: readAttachmentList(item.attachments)
  };
}

function readObject(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is invalid`);
  }
  return value as Record<string, unknown>;
}

function readString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${label} is invalid`);
  }
  return value.trim();
}

function readOptionalString(value: unknown): string | null {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "string" || !value.trim()) {
    throw new Error("optional string is invalid");
  }
  return value.trim();
}

/**
 * 要求对象只包含给定字段，防止 Backend 新字段在 Desktop 未审查时静默进入状态。
 *
 * @param value 已确认是普通对象的协议值。
 * @param allowedKeys 当前结构允许出现的字段。
 * @param label 错误信息使用的结构名称。
 */
function assertOnlyKeys(
  value: Record<string, unknown>,
  allowedKeys: readonly string[],
  label: string
): void {
  if (Object.keys(value).some(key => !allowedKeys.includes(key))) {
    throw new Error(`${label} fields are invalid`);
  }
}

/**
 * 要求对象包含每个必填字段，避免缺失值被可选读取逻辑吞掉。
 *
 * @param value 已确认是普通对象的协议值。
 * @param requiredKeys 当前结构必须拥有的字段。
 * @param label 错误信息使用的结构名称。
 */
function assertRequiredKeys(
  value: Record<string, unknown>,
  requiredKeys: readonly string[],
  label: string
): void {
  if (requiredKeys.some(key => !Object.hasOwn(value, key))) {
    throw new Error(`${label} fields are invalid`);
  }
}

/**
 * 要求对象字段集合完全匹配契约。
 *
 * @param value 已确认是普通对象的协议值。
 * @param expectedKeys 当前结构必须且只能拥有的字段。
 * @param label 错误信息使用的结构名称。
 */
function assertExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
  label: string
): void {
  assertOnlyKeys(value, expectedKeys, label);
  assertRequiredKeys(value, expectedKeys, label);
}
