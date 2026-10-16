import { describe, expect, it } from "vitest";
import { parseRuntimeInitializationStatus } from "./runtime-status";

const initial = {
  runtime_id: "d01f2fa6-8e4d-430f-8b72-4bc3d02051ec", operation_id: "88c655af-c333-4e30-b424-a1fb67794847",
  revision: 1, state: "initializing", phase: "input_models", message: "正在加载语音输入模型", retryable: false,
};

describe("初始化状态契约", () => {
  it.each([
    { runtime_id: "process" }, { operation_id: null }, { revision: -1 },
    { revision: Number.MAX_SAFE_INTEGER + 1 }, { state: "ready" }, { phase: "unknown" },
    { retryable: true }, { message: " " }, { extra: "unexpected" },
  ])("拒绝无法确定身份、顺序或实际阶段的快照 %j", change => {
    expect(() => parseRuntimeInitializationStatus({ ...initial, ...change })).toThrow();
  });

  it("接受首次配置尚未完成的idle以及初始化失败", () => {
    expect(parseRuntimeInitializationStatus({ ...initial, operation_id: null, state: "idle", phase: "idle" }).state).toBe("idle");
    expect(parseRuntimeInitializationStatus({ ...initial, state: "failed", phase: "failed", retryable: true }).retryable).toBe(true);
  });

});
