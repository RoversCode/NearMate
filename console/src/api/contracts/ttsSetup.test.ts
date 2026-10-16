import { describe, expect, it } from "vitest";

import { parseTtsCapabilities, parseTtsConfiguration, parseTtsModelCatalog, parseTtsModelDownloadStatus } from "./ttsSetup";

describe("语音执行方式契约", () => {
  it("接受 CUDA ONNX 探测结果", () => {
    const capabilities = {
      backends: [
        { kind: "onnx", backend: "cuda", available: true, evidence: "matmul" },
        { kind: "llama", backend: "cuda", available: true, evidence: "device_initialization" }
      ],
      recommendation: { onnx_provider: "cuda", llama_backend: "cuda" }
    };

    expect(parseTtsCapabilities(capabilities)).toEqual(capabilities);
  });

  it("接受已确认的 CUDA ONNX 执行配置", () => {
    expect(parseTtsConfiguration({
      confirmed: true,
      selection: { onnx_provider: "cuda", llama_backend: "cuda" },
      model_directory: "D:\\NearMate Models",
      capabilities: null
    }).selection).toEqual({ onnx_provider: "cuda", llama_backend: "cuda" });
  });
});

describe("模型文件状态契约", () => {
  const model = { model_id: "s2-pro-4b-gguf", display_name: "S2-Pro", repository: "ChengHee/s2-pro-4b-gguf",
    estimated_size_bytes: 1234, verification_status: "数值与实机未验证", download_available: true };
  const download = { phase: "completed", task_id: "download-1", model_id: model.model_id, progress: null,
    error: null, can_cancel: false, can_retry: false, can_confirm: false, is_ready: false };

  it("旧目录响应缺少磁盘证据时保持未知", () => {
    const catalog = parseTtsModelCatalog({ provider: "local_streamvox", models: [model], configured_model: model.model_id });
    expect(catalog.models[0]).toMatchObject({ local_state: "unknown", installed_size_bytes: null,
      total_size_bytes: null, integrity_status: "not_verified", is_current: true });
    expect(catalog.download_task).toBeNull();
  });

  it("文件已下载与当前选择、完整校验分别保留", () => {
    const catalog = parseTtsModelCatalog({ provider: "local_streamvox", configured_model: null, download_task: download,
      models: [{ ...model, local_state: "downloaded", installed_size_bytes: 1234, total_size_bytes: 1234,
        integrity_status: "not_verified", is_current: false }] });
    expect(catalog.models[0]).toMatchObject({ local_state: "downloaded", integrity_status: "not_verified", is_current: false });
    expect(catalog.download_task).toEqual(download);
  });

  it.each([{ local_state: "ready" }, { installed_size_bytes: -1 }, { installed_size_bytes: Number.MAX_SAFE_INTEGER + 1 },
    { integrity_status: true }, { is_current: "true" }])("拒绝错误磁盘字段：%j", invalid => {
    expect(() => parseTtsModelCatalog({ provider: "local_streamvox", models: [{ ...model, ...invalid }], configured_model: null })).toThrow();
  });

  it("拒绝给纯下载任务授予语音就绪或试听确认资格", () => {
    expect(parseTtsModelDownloadStatus(download)).toEqual(download);
    expect(() => parseTtsModelDownloadStatus({ ...download, is_ready: true })).toThrow();
    expect(() => parseTtsModelDownloadStatus({ ...download, can_confirm: true })).toThrow();
    expect(() => parseTtsModelDownloadStatus({ ...download, phase: "awaiting_confirmation" })).toThrow();
  });
});
