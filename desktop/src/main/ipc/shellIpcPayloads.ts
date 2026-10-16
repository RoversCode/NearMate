import type {
  AvatarPetBounds,
  Live2dModelSwitchRequest,
  Live2dModelSwitchState
} from "../../shared/live2dModelTypes";
import { isStandardUuid } from "../../shared/uuid";

/**
 * 从桌宠点击载荷中读取需要定位的后台待答请求编号。
 *
 * @param payload avatar renderer 传入的未知 IPC 载荷。
 * @returns 只含标准 UUID 的请求编号；字段缺失、多余或格式非法时返回空。
 */
export function readBackgroundAgentInteractionRequestId(payload: unknown): string | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }

  const candidate = payload as Record<string, unknown>;
  if (Object.keys(candidate).length !== 1 || typeof candidate.requestId !== "string") {
    return null;
  }

  return isStandardUuid(candidate.requestId) ? candidate.requestId : null;
}

/**
 * readLive2dModelSwitchRequest 从 IPC payload 中读取合法模型切换请求。
 *
 * @param payload renderer 传入的未知 IPC payload。
 * @returns 保留可选标准 UUID 的合法切换请求，非法时返回 null。
 */
export function readLive2dModelSwitchRequest(payload: unknown): Live2dModelSwitchRequest | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<Live2dModelSwitchRequest>;
  const normalizedModelId = typeof candidate.modelId === "string" ? candidate.modelId.trim() : "";

  if (!normalizedModelId || (candidate.requestId !== undefined && !isStandardUuid(candidate.requestId))) {
    return null;
  }

  return {
    modelId: normalizedModelId,
    ...(candidate.requestId === undefined ? {} : { requestId: candidate.requestId })
  };
}

/**
 * readLive2dModelSwitchState 从 avatar 回传状态里提取稳定结构。
 *
 * @param payload avatar renderer 回传的未知 IPC payload。
 * @returns 保留原请求编号的合法模型切换状态，非法时返回 null。
 */
export function readLive2dModelSwitchState(payload: unknown): Live2dModelSwitchState | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<Live2dModelSwitchState>;
  const normalizedModelId = typeof candidate.modelId === "string" ? candidate.modelId.trim() : "";

  if (!normalizedModelId || (candidate.requestId !== undefined && !isStandardUuid(candidate.requestId))) {
    return null;
  }

  if (candidate.status === "switching" || candidate.status === "succeeded") {
    return {
      status: candidate.status,
      modelId: normalizedModelId,
      ...(candidate.requestId === undefined ? {} : { requestId: candidate.requestId })
    };
  }

  if (candidate.status !== "failed" || typeof candidate.reason !== "string") {
    return null;
  }

  return {
    modelId: normalizedModelId,
    status: "failed",
    reason: candidate.reason,
    ...(candidate.requestId === undefined ? {} : { requestId: candidate.requestId })
  };
}

/**
 * readAvatarPetBounds 从 IPC payload 中读取人物可见边界。
 *
 * @param payload renderer 上报的未知人物边界。
 * @returns 有限数字边界，非法时返回 null。
 */
export function readAvatarPetBounds(payload: unknown): AvatarPetBounds | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }

  const candidate = payload as Partial<AvatarPetBounds>;

  if (
    typeof candidate.x !== "number" ||
    typeof candidate.y !== "number" ||
    typeof candidate.width !== "number" ||
    typeof candidate.height !== "number" ||
    !Number.isFinite(candidate.x) ||
    !Number.isFinite(candidate.y) ||
    !Number.isFinite(candidate.width) ||
    !Number.isFinite(candidate.height)
  ) {
    return null;
  }

  return {
    x: candidate.x,
    y: candidate.y,
    width: candidate.width,
    height: candidate.height
  };
}
