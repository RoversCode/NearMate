import type { TtsModelDescriptor, TtsSetupTask } from "../api/contracts/bootstrap";
import type { TtsCapabilities, TtsConfiguration } from "../api/contracts/ttsSetup";

export const ttsCapabilitiesFixture: TtsCapabilities = {
  backends: [
    { kind: "onnx", backend: "directml", available: true },
    { kind: "onnx", backend: "cpu", available: true },
    { kind: "llama", backend: "cuda", available: false, reason: "缺少 CUDA Runtime，请在启动检查中修复" },
    { kind: "llama", backend: "vulkan", available: true },
    { kind: "llama", backend: "cpu", available: true }
  ], recommendation: { onnx_provider: "directml", llama_backend: "vulkan" }
};
export const ttsModelsFixture: TtsModelDescriptor[] = [
  { model_id: "qwen3-tts-clone-0.6b-gguf", display_name: "Qwen3 TTS 0.6B", repository: "nearmate/qwen3", estimated_size_bytes: 1024 ** 3, verification_status: "数值与实机未验证", download_available: true, sdk_version: "1.2.6" },
  { model_id: "moss-tts-nano-onnx", display_name: "MOSS TTS Nano", repository: "nearmate/moss", estimated_size_bytes: 2 * 1024 ** 3, verification_status: "数值与实机未验证", download_available: true, sdk_version: "1.2.6", backend_note: "DirectML 模式下音频解码固定使用 CPU" }
];

/**
 * 创建首次配置测试的配置快照。
 * Args:
 *   confirmed: 是否已有用户确认的执行方式。
 * Returns:
 *   带绝对模型目录的配置。
 */
export function createTtsConfigurationFixture(confirmed = true): TtsConfiguration {
  return { confirmed, selection: confirmed ? ttsCapabilitiesFixture.recommendation : null, model_directory: "D:\\NearMate\\models", capabilities: null };
}

/**
 * 创建真实接口形状的语音任务快照。
 * Args:
 *   overrides: 当前场景需要覆盖的阶段和动作资格。
 * Returns:
 *   完整任务快照。
 */
export function createTtsTaskFixture(overrides: Partial<TtsSetupTask> = {}): TtsSetupTask {
  return { task_id: null, model_id: null, phase: "unconfigured", progress: null, error: null, can_cancel: false, can_confirm: false, can_retry: false, ...overrides };
}
