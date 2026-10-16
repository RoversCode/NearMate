import {
  type CharacterCard,
  type CharacterCardClonePayload,
  type CharacterCardPayload,
  type CharacterCardStreamVoxExampleSet,
  type CharacterCardVoiceReferenceUploadResult,
  type CharacterCardsPayload,
  type CharacterVoiceStatusPayload,
  isCharacterCardPayload,
  isCharacterCardStreamVoxExampleSet,
  isCharacterCardVoiceReferenceUploadResult,
  isCharacterCardsPayload,
  isCharacterVoiceStatusPayload
} from "../contracts/characterCards";
import { normalizeConsoleSettings } from "../contracts/settings";
import { fetchJson, fetchJsonWithInit, postJson } from "../httpClient";

/**
 * 读取角色卡列表和当前激活卡槽。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   全部角色卡、当前激活卡 id 和共享世界观只读文本。
 */
export async function fetchCharacterCards(baseUrl: string): Promise<CharacterCardsPayload> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/character-cards", "读取角色卡失败");

  if (!isCharacterCardsPayload(payload)) {
    throw new Error("读取角色卡失败：响应格式不符合控制台契约");
  }

  return payload;
}

/**
 * 读取当前激活的角色卡。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   当前角色卡完整内容，以及 effect_message 与校验结果。
 */
export async function fetchActiveCharacterCard(baseUrl: string): Promise<CharacterCardPayload> {
  const payload = await fetchJson<unknown>(baseUrl, "/api/character-cards/active", "读取当前角色卡失败");

  if (!isCharacterCardPayload(payload)) {
    throw new Error("读取当前角色卡失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterCardPayloadSettings(payload);
}

/**
 * 保存指定角色卡的完整内容。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *   card: 完整角色卡对象。
 *
 * Returns:
 *   后端保存后的角色卡响应。
 */
export async function updateCharacterCard(
  baseUrl: string,
  cardId: string,
  card: CharacterCard
): Promise<CharacterCardPayload> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}`,
    {
      method: "PUT",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(card)
    },
    "保存角色卡失败"
  );

  if (!isCharacterCardPayload(payload)) {
    throw new Error("保存角色卡失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterCardPayloadSettings(payload);
}

/**
 * 根据当前页面草稿生成指定 StreamVox 模型槽位的角色示例。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 当前编辑卡编号。
 *   targetModel: generic 或具体模型名。
 *   card: 尚未保存的页面草稿。
 *
 * Returns:
 *   结构化示例集合。
 */
export async function generateCharacterCardStreamVoxExamples(
  baseUrl: string,
  cardId: string,
  targetModel: string,
  card: CharacterCard
): Promise<CharacterCardStreamVoxExampleSet> {
  const payload = await postJson<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/streamvox-examples/generate`,
    {
      card,
      target_model: targetModel
    },
    "生成 StreamVox 示例失败"
  );

  if (!isCharacterCardStreamVoxExampleSet(payload)) {
    throw new Error("生成 StreamVox 示例失败：响应格式不符合控制台契约");
  }
  return payload;
}

/**
 * 激活指定角色卡。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *
 * Returns:
 *   切换后的当前角色卡状态。
 */
export async function activateCharacterCard(baseUrl: string, cardId: string): Promise<CharacterCardPayload> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/activate`,
    {
      method: "POST"
    },
    "激活角色卡失败"
  );

  if (!isCharacterCardPayload(payload)) {
    throw new Error("激活角色卡失败：响应格式不符合控制台契约");
  }

  return payload;
}

/**
 * 读取角色语音匹配状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *
 * Returns:
 *   后端计算出的语音匹配状态。
 */
export async function fetchCharacterCardVoiceStatus(baseUrl: string, cardId: string): Promise<CharacterVoiceStatusPayload> {
  const payload = await fetchJson<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/voice-status`,
    "读取角色语音状态失败"
  );

  if (!isCharacterVoiceStatusPayload(payload)) {
    throw new Error("读取角色语音状态失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterVoiceStatusSettings(payload);
}

/**
 * 应用角色卡声明的 StreamVox 语音。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *
 * Returns:
 *   应用后的语音匹配状态。
 */
export async function applyCharacterCardVoice(baseUrl: string, cardId: string): Promise<CharacterVoiceStatusPayload> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/voice/apply`,
    {
      method: "POST"
    },
    "应用角色语音失败"
  );

  if (!isCharacterVoiceStatusPayload(payload)) {
    throw new Error("应用角色语音失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterVoiceStatusSettings(payload);
}

/**
 * 从当前 StreamVox runtime 捕获语音配置并写入角色卡。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *
 * Returns:
 *   更新后的角色卡响应。
 */
export async function captureCharacterCardVoiceFromRuntime(baseUrl: string, cardId: string): Promise<CharacterCardPayload> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/voice/capture-from-runtime`,
    {
      method: "POST"
    },
    "更新角色卡语音失败"
  );

  if (!isCharacterCardPayload(payload)) {
    throw new Error("更新角色卡语音失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterCardPayloadSettings(payload);
}

/**
 * 测算角色卡字幕语速。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *
 * Returns:
 *   写入字幕校准结果后的角色卡响应。
 */
export async function calibrateCharacterCardVoiceCaption(baseUrl: string, cardId: string): Promise<CharacterCardPayload> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/voice-caption-calibration`,
    {
      method: "POST"
    },
    "测算角色字幕语速失败"
  );

  if (!isCharacterCardPayload(payload)) {
    throw new Error("测算角色字幕语速失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterCardPayloadSettings(payload);
}

/**
 * 上传角色卡参考音频资产。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标卡编号。
 *   audioFile: 用户选择的参考音频。
 *
 * Returns:
 *   卡内相对路径和提示信息。
 */
export async function uploadCharacterCardVoiceReference(
  baseUrl: string,
  cardId: string,
  audioFile: File
): Promise<CharacterCardVoiceReferenceUploadResult> {
  const formData = new FormData();
  formData.append("audio_file", audioFile);

  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/voice/reference-audio`,
    {
      method: "POST",
      body: formData
    },
    "上传角色卡参考音频失败"
  );

  if (!isCharacterCardVoiceReferenceUploadResult(payload)) {
    throw new Error("上传角色卡参考音频失败：响应格式不符合控制台契约");
  }

  return payload;
}

/**
 * 复制指定角色卡。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   payload: 复制目标、名称和标题。
 *
 * Returns:
 *   新生成的角色卡完整响应。
 */
export async function cloneCharacterCard(
  baseUrl: string,
  payload: CharacterCardClonePayload
): Promise<CharacterCardPayload> {
  const responsePayload = await postJson<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(payload.source_card_id)}/clone`,
    {
      card_id: payload.card_id,
      name: payload.name,
      title: payload.title
    },
    "复制角色卡失败"
  );

  if (!isCharacterCardPayload(responsePayload)) {
    throw new Error("复制角色卡失败：响应格式不符合控制台契约");
  }

  return normalizeCharacterCardPayloadSettings(responsePayload);
}

/**
 * 上传或替换角色卡封面。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *   imageFile: 用户选择的 PNG/JPEG 图片。
 *
 * Returns:
 *   后端更新后的角色卡响应。
 */
export async function uploadCharacterCardCover(
  baseUrl: string,
  cardId: string,
  imageFile: File
): Promise<CharacterCardPayload> {
  const formData = new FormData();
  formData.append("image_file", imageFile);

  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/cover`,
    {
      method: "POST",
      body: formData
    },
    "上传角色卡封面失败"
  );
  if (!isCharacterCardPayload(payload)) {
    throw new Error("上传角色卡封面失败：响应格式不符合控制台契约");
  }
  return normalizeCharacterCardPayloadSettings(payload);
}

/**
 * 删除角色卡封面。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   cardId: 目标角色卡编号。
 *
 * Returns:
 *   后端清空封面后的角色卡响应。
 */
export async function deleteCharacterCardCover(baseUrl: string, cardId: string): Promise<CharacterCardPayload> {
  const payload = await fetchJsonWithInit<unknown>(
    baseUrl,
    `/api/character-cards/${encodeURIComponent(cardId)}/cover`,
    { method: "DELETE" },
    "删除角色卡封面失败"
  );

  if (!isCharacterCardPayload(payload)) {
    throw new Error("删除角色卡封面失败：响应格式不符合控制台契约");
  }
  return normalizeCharacterCardPayloadSettings(payload);
}

/**
 * 复制角色卡响应中的可选设置快照，供统一设置状态直接接纳。
 *
 * Args:
 *   payload: 已通过角色卡响应契约校验的 payload。
 *
 * Returns:
 *   保留角色卡字段，并复制可选的规范化设置快照。
 */
function normalizeCharacterCardPayloadSettings(payload: CharacterCardPayload): CharacterCardPayload {
  return payload.settings === undefined
    ? payload
    : {
        ...payload,
        settings: normalizeConsoleSettings(payload.settings)
      };
}

/**
 * 复制角色语音响应中的可选设置快照，供统一设置状态直接接纳。
 *
 * Args:
 *   payload: 已通过语音状态响应契约校验的 payload。
 *
 * Returns:
 *   保留语音匹配字段，并复制可选的规范化设置快照。
 */
function normalizeCharacterVoiceStatusSettings(payload: CharacterVoiceStatusPayload): CharacterVoiceStatusPayload {
  return payload.settings === undefined
    ? payload
    : {
        ...payload,
        settings: normalizeConsoleSettings(payload.settings)
      };
}
