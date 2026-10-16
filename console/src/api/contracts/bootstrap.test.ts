import { describe, expect, it } from "vitest";

import { isBootstrapStatus } from "./bootstrap";

/**
 * 构造四键齐全的初始化状态载荷。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   符合新契约的 llm、voice、has_qweather_api_key、is_initialized 四键载荷。
 */
function createFourKeyPayload(): Record<string, unknown> {
  return {
    llm: {
      is_ready: true,
      missing: [],
      errors: []
    },
    voice: {
      provider: "local_streamvox",
      is_ready: true,
      phase: "ready",
      model_id: "qwen3-tts-clone-0.6b-gguf",
      role_name: "huohua",
      has_confirmed_voice: true
    },
    has_qweather_api_key: false,
    is_initialized: true
  };
}

describe("isBootstrapStatus", () => {
  it("接受四键齐全的状态载荷", () => {
    expect(isBootstrapStatus(createFourKeyPayload())).toBe(true);
  });

  it("拒绝缺少历史语音确认标记的状态", () => {
    const payload = createFourKeyPayload();
    delete (payload.voice as Record<string, unknown>).has_confirmed_voice;

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it("拒绝非布尔的历史语音标记", () => {
    const payload = createFourKeyPayload();
    (payload.voice as Record<string, unknown>).has_confirmed_voice = "yes";

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it("缺少 voice 键时拒绝", () => {
    const payload = createFourKeyPayload();
    delete payload.voice;

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it.each(["llm", "has_qweather_api_key", "is_initialized"])("缺少 %s 键时拒绝", (key) => {
    const payload = createFourKeyPayload();
    delete payload[key];

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it("voice 缺少绑定字段时拒绝", () => {
    const payload = createFourKeyPayload();
    payload.voice = { provider: "local_streamvox", is_ready: false, phase: "unconfigured" };

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it("llm 夹带内部字段时拒绝", () => {
    const payload = createFourKeyPayload();
    payload.llm = { ...(payload.llm as object), provider_debug: "internal" };

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it.each([
    ["task_id", "internal-task"],
    ["error", "原始异常：C:/secret/trace.log"]
  ])("voice 夹带内部字段 %s 时拒绝", (key, value) => {
    const payload = createFourKeyPayload();
    payload.voice = { ...(payload.voice as object), [key]: value };

    expect(isBootstrapStatus(payload)).toBe(false);
  });

  it("载荷夹带已下线的 tts 键时拒绝", () => {
    // 旧字段一旦混入就说明前后端契约错位，不能让 UI 静默接受半旧快照。
    expect(isBootstrapStatus({ ...createFourKeyPayload(), tts: { voice_ready: true } })).toBe(false);
  });
});
