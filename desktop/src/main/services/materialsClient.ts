import {
  readMaterialContextSnapshot,
  type MaterialContextSnapshot
} from "../../shared/materialContext";
import type { MaterialAdmissionIssue, MaterialIssueCategory } from "../../shared/materialFeedback";

export type MaterialItemPayload = {
  kind:
    | "clipboard_text"
    | "clipboard_image"
    | "dragged_file"
    | "dragged_folder"
    | "selected_text"
    | "selected_image"
    | "selected_web";
  source: string;
  display_name: string;
  path: string;
  managed_asset_id?: string | null;
};

export type MaterialAdmission = {
  submittedCount: number;
  addedCount: number;
  unsupportedCount: number;
  unreadableCount: number;
  duplicateCount: number;
  issues: MaterialAdmissionIssue[];
};

type StagedMaterialsResult = {
  admission: MaterialAdmission;
  materialContext: MaterialContextSnapshot;
};

type MaterialDeleteResult = {
  deletedMaterialId: string;
  deletedDisplayName: string;
  releasedManagedAssetIds: string[];
  materialContext: MaterialContextSnapshot;
};

export type MaterialsClient = {
  appendStagedMaterials(items: MaterialItemPayload[]): Promise<StagedMaterialsResult>;
  readCurrentMaterialContext(): Promise<MaterialContextSnapshot>;
  deleteStagedMaterial(materialId: string): Promise<MaterialDeleteResult>;
  clearStagedMaterials(): Promise<{ releasedManagedAssetIds: string[]; materialContext: MaterialContextSnapshot }>;
  forgetRecentMaterial(path: string): Promise<MaterialContextSnapshot>;
};

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

type CreateMaterialsClientOptions = {
  httpBaseUrl: string;
  fetch?: FetchLike;
};

/**
 * 创建本轮材料与 turn 激活的控制面客户端。
 *
 * @param options 控制服务 HTTP 根地址与可注入 fetch。
 * @returns 只负责两个本轮材料写入动作的客户端。
 */
export function createMaterialsClient(options: CreateMaterialsClientOptions): MaterialsClient {
  const normalizedHttpBaseUrl = options.httpBaseUrl.replace(/\/+$/, "");
  const fetchImplementation = options.fetch ?? globalThis.fetch.bind(globalThis);

  return {
    async appendStagedMaterials(items): Promise<StagedMaterialsResult> {
      if (items.length === 0) {
        throw new Error("staged material append requires at least one item");
      }
      const response = await fetchImplementation(`${normalizedHttpBaseUrl}/api/materials/turn/staged`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items })
      });
      if (!response.ok) {
        throw new Error(`staged turn materials request failed: ${response.status}`);
      }

      const payload: unknown = await response.json();
      return readStagedMaterialsResult(payload, items.length);
    },

    async readCurrentMaterialContext(): Promise<MaterialContextSnapshot> {
      const response = await fetchImplementation(`${normalizedHttpBaseUrl}/api/materials/contexts/current`);
      if (!response.ok) {
        throw new Error(`current material context request failed: ${response.status}`);
      }
      return readMaterialContextSnapshot(await response.json());
    },

    async deleteStagedMaterial(materialId): Promise<MaterialDeleteResult> {
      const response = await fetchImplementation(
        `${normalizedHttpBaseUrl}/api/materials/turn/staged/${encodeURIComponent(materialId)}`,
        { method: "DELETE" }
      );
      if (!response.ok) {
        throw new Error(`staged material delete request failed: ${response.status}`);
      }

      return readMaterialDeleteResult(await response.json(), materialId);
    },

    async clearStagedMaterials() {
      const response = await fetchImplementation(`${normalizedHttpBaseUrl}/api/materials/turn/staged/clear`, {
        method: "POST"
      });
      if (!response.ok) {
        throw new Error(`staged materials clear request failed: ${response.status}`);
      }
      const payload = readObject(await response.json(), "staged materials clear response");
      if (payload.accepted !== true) {
        throw new Error("staged materials clear response was not accepted");
      }
      return {
        releasedManagedAssetIds: readStringList(payload.released_managed_asset_ids),
        materialContext: readMaterialContextSnapshot(payload.material_context)
      };
    },

    async forgetRecentMaterial(path): Promise<MaterialContextSnapshot> {
      const normalizedPath = path.trim();
      if (!normalizedPath) {
        throw new Error("recent material forget requires path");
      }
      const response = await fetchImplementation(`${normalizedHttpBaseUrl}/api/materials/recent/forget`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: normalizedPath })
      });
      if (!response.ok) {
        throw new Error(`recent material forget request failed: ${response.status}`);
      }
      const payload = readObject(await response.json(), "recent material forget response");
      if (payload.accepted !== true) {
        throw new Error("recent material forget response was not accepted");
      }
      return readMaterialContextSnapshot(payload.material_context);
    }
  };
}

/**
 * 从后端实际接纳材料中提取项目副本编号。
 *
 * @param payload staged 写入接口响应。
 * @returns 后端已经持有引用的受管资产编号。
 */
function readStagedMaterialsResult(payload: unknown, submittedCount: number): StagedMaterialsResult {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("staged turn materials response is invalid");
  }
  const response = payload as {
    accepted?: unknown;
    admission?: unknown;
    material_context?: unknown;
  };
  if (response.accepted !== true) {
    throw new Error("staged turn materials response was not accepted");
  }
  const admission = readMaterialAdmission(response.admission);
  if (admission.submittedCount !== submittedCount) {
    throw new Error("staged turn materials response submitted count is inconsistent");
  }

  return {
    admission,
    materialContext: readMaterialContextSnapshot(response.material_context)
  };
}

function readMaterialDeleteResult(payload: unknown, requestedMaterialId: string): MaterialDeleteResult {
  const response = readObject(payload, "staged material delete response");
  if (response.accepted !== true || response.deleted_material_id !== requestedMaterialId) {
    throw new Error("staged material delete response does not match request");
  }
  const deletedMaterial = readObject(response.deleted_material, "deleted material");
  if (deletedMaterial.material_id !== requestedMaterialId) {
    throw new Error("staged material delete response contains inconsistent deleted material");
  }
  return {
    deletedMaterialId: requestedMaterialId,
    deletedDisplayName: readRequiredString(deletedMaterial.display_name, "deleted material display name"),
    releasedManagedAssetIds: readStringList(response.released_managed_asset_ids),
    materialContext: readMaterialContextSnapshot(response.material_context)
  };
}

function readMaterialAdmission(payload: unknown): MaterialAdmission {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("staged turn materials response is missing admission");
  }
  const candidate = payload as Record<string, unknown>;
  const values = [
    candidate.submitted_count,
    candidate.added_count,
    candidate.unsupported_count,
    candidate.unreadable_count,
    candidate.duplicate_count
  ];
  if (!values.every(isNonNegativeInteger)) {
    throw new Error("staged turn materials admission contains invalid counts");
  }
  const [submittedCount, addedCount, unsupportedCount, unreadableCount, duplicateCount] = values as number[];
  if (addedCount + unsupportedCount + unreadableCount + duplicateCount !== submittedCount) {
    throw new Error("staged turn materials admission totals are inconsistent");
  }
  const issues = readMaterialAdmissionIssues(candidate.issues, submittedCount);
  const issueCounts = {
    unsupported: issues.filter(item => item.category === "unsupported").length,
    unreadable: issues.filter(item => item.category === "unreadable").length,
    duplicate: issues.filter(item => item.category === "duplicate").length
  };
  if (issueCounts.unsupported !== unsupportedCount
    || issueCounts.unreadable !== unreadableCount
    || issueCounts.duplicate !== duplicateCount) {
    throw new Error("staged turn materials admission issue counts are inconsistent");
  }
  return {
    submittedCount,
    addedCount,
    unsupportedCount,
    unreadableCount,
    duplicateCount,
    issues
  };
}

function readMaterialAdmissionIssues(value: unknown, submittedCount: number): MaterialAdmissionIssue[] {
  if (!Array.isArray(value)) {
    throw new Error("staged turn materials admission is missing issues");
  }
  let previousIndex = -1;
  return value.map(item => {
    const candidate = readObject(item, "material admission issue");
    if (!isNonNegativeInteger(candidate.submitted_index)
      || candidate.submitted_index >= submittedCount
      || candidate.submitted_index <= previousIndex) {
      throw new Error("material admission issues are not in submission order");
    }
    previousIndex = candidate.submitted_index;
    const category = candidate.category;
    if (category !== "unsupported" && category !== "unreadable" && category !== "duplicate") {
      throw new Error("material admission issue category is invalid");
    }
    return {
      submittedIndex: candidate.submitted_index,
      displayName: readRequiredString(candidate.display_name, "material issue display name"),
      category: category as MaterialIssueCategory,
      reason: readRequiredString(candidate.reason, "material issue reason")
    };
  });
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function readObject(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} is invalid`);
  }
  return value as Record<string, unknown>;
}

function readStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    throw new Error("managed asset id list is invalid");
  }
  return value.flatMap(item => typeof item === "string" && item.trim() ? [item.trim()] : []);
}

function readRequiredString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${label} is invalid`);
  }
  return value.trim();
}
