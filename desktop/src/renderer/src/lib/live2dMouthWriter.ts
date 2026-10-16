import { clamp } from "./live2dAudioAnalysis";
import type {
  Live2dLipSyncModel,
  Live2dLipSyncWriterKind,
  Live2dLipSyncWriteResult
} from "./live2dLipSyncTypes";

const DEFAULT_LIP_SYNC_PARAMETER_IDS = ["ParamMouthOpenY", "PARAM_MOUTH_OPEN_Y"] as const;
const DEFAULT_LIP_SYNC_GAIN = 1.5;
const DEFAULT_LIP_SYNC_WEIGHT = 0.4;

/**
 * readLive2dLipSyncParameterIds 读取当前模型声明的嘴型参数。
 *
 * Args:
 *   model: 已加载的 Live2D 模型实例。
 *
 * Returns:
 *   可写入嘴型值的参数 ID 列表；模型未声明时返回常见兜底参数。
 */
export function readLive2dLipSyncParameterIds(model: Live2dLipSyncModel): string[] {
  const motionManager = toRecord(model.internalModel?.motionManager);
  const rawLipSyncIds = motionManager?.lipSyncIds;
  const lipSyncIds = Array.isArray(rawLipSyncIds)
    ? rawLipSyncIds.filter((id): id is string => typeof id === "string" && id.length > 0)
    : [];

  if (lipSyncIds.length > 0) {
    return lipSyncIds;
  }

  return [...DEFAULT_LIP_SYNC_PARAMETER_IDS];
}

/**
 * writeLive2dMouthOpenValue 把嘴型值写入模型的所有唇形同步参数。
 *
 * Args:
 *   model: 已加载的 Live2D 模型实例。
 *   value: 归一化后的嘴部开合值。
 *
 * Returns:
 *   写入结果与实际使用的 SDK writer 类型。
 */
export function writeLive2dMouthOpenValue(
  model: Live2dLipSyncModel,
  value: number
): Live2dLipSyncWriteResult {
  const internalModel = model.internalModel;
  if (!internalModel) {
    return {
      reason: "missing_internal_model",
      writerKind: "none",
      written: false
    };
  }

  const coreModel = toRecord(internalModel.coreModel);
  const rawWeight = toRecord(internalModel.options)?.lipSyncWeight;
  const weight = typeof rawWeight === "number" ? rawWeight : DEFAULT_LIP_SYNC_WEIGHT;
  const mouthValue = clamp(value, 0, 1);
  const parameterIds = readLive2dLipSyncParameterIds(model);
  const setParameterValueById = coreModel?.setParameterValueById;
  if (typeof setParameterValueById === "function") {
    for (const parameterId of parameterIds) {
      const resolvedParameterId =
        typeof internalModel.getIdSafe === "function"
          ? internalModel.getIdSafe(parameterId)
          : parameterId;
      setParameterValueById.call(coreModel, resolvedParameterId, mouthValue, 1);
    }

    return {
      writerKind: "cubism",
      written: true
    };
  }

  const addParameterValueById = coreModel?.addParameterValueById;

  if (typeof addParameterValueById === "function") {
    for (const parameterId of parameterIds) {
      const resolvedParameterId =
        typeof internalModel.getIdSafe === "function"
          ? internalModel.getIdSafe(parameterId)
          : parameterId;
      addParameterValueById.call(coreModel, resolvedParameterId, mouthValue, weight);
    }

    return {
      writerKind: "cubism",
      written: true
    };
  }

  const setParamFloat = coreModel?.setParamFloat;
  const getParamIndex = coreModel?.getParamIndex;
  if (typeof setParamFloat === "function" && typeof getParamIndex === "function") {
    for (const parameterId of parameterIds) {
      setParamFloat.call(coreModel, getParamIndex.call(coreModel, parameterId), mouthValue, 1);
    }

    return {
      writerKind: "legacy",
      written: true
    };
  }

  const addToParamFloat = coreModel?.addToParamFloat;
  if (typeof addToParamFloat === "function" && typeof getParamIndex === "function") {
    for (const parameterId of parameterIds) {
      addToParamFloat.call(coreModel, getParamIndex.call(coreModel, parameterId), mouthValue, weight);
    }

    return {
      writerKind: "legacy",
      written: true
    };
  }

  return {
    reason: "missing_mouth_parameter_writer",
    writerKind: "none",
    written: false
  };
}

export function toRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }

  return value as Record<string, unknown>;
}

export function readLive2dLipSyncGain(model: Live2dLipSyncModel): number {
  const rawGain = toRecord(model.internalModel?.options)?.lipSyncGain;

  return typeof rawGain === "number" ? rawGain : DEFAULT_LIP_SYNC_GAIN;
}

export function resolveLive2dMouthParameterWriterKind(
  model: Live2dLipSyncModel
): Live2dLipSyncWriterKind {
  const internalModel = model.internalModel;
  const coreModel = toRecord(internalModel?.coreModel);

  if (!internalModel || !coreModel) {
    return "none";
  }

  if (
    typeof coreModel.setParameterValueById === "function" ||
    typeof coreModel.addParameterValueById === "function"
  ) {
    return "cubism";
  }

  if (
    (typeof coreModel.setParamFloat === "function" ||
      typeof coreModel.addToParamFloat === "function") &&
    typeof coreModel.getParamIndex === "function"
  ) {
    return "legacy";
  }

  return "none";
}

export function readLive2dMouthParameterDiagnostic(
  model: Live2dLipSyncModel
): {
  parameterId: string | null;
  parameterValue: number | null;
} {
  const parameterId = readLive2dLipSyncParameterIds(model)[0] ?? null;
  if (!parameterId) {
    return {
      parameterId: null,
      parameterValue: null
    };
  }

  const internalModel = model.internalModel;
  const coreModel = toRecord(internalModel?.coreModel);
  if (!internalModel || !coreModel) {
    return {
      parameterId,
      parameterValue: null
    };
  }

  try {
    const getParameterValueById = coreModel.getParameterValueById;
    if (typeof getParameterValueById === "function") {
      const resolvedParameterId =
        typeof internalModel.getIdSafe === "function"
          ? internalModel.getIdSafe(parameterId)
          : parameterId;
      const value = getParameterValueById.call(coreModel, resolvedParameterId);

      return {
        parameterId,
        parameterValue: typeof value === "number" ? value : null
      };
    }

    const getParamFloat = coreModel.getParamFloat;
    const getParamIndex = coreModel.getParamIndex;
    if (typeof getParamFloat === "function" && typeof getParamIndex === "function") {
      const value = getParamFloat.call(coreModel, getParamIndex.call(coreModel, parameterId));

      return {
        parameterId,
        parameterValue: typeof value === "number" ? value : null
      };
    }
  } catch {
    /**
     * 业务意图：参数快照只是诊断辅助，读取失败不应影响正常对话和口型写入。
     */
  }

  return {
    parameterId,
    parameterValue: null
  };
}
