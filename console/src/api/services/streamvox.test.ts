import { describe, expect, it, vi } from "vitest";

import {
  createStreamVoxRole,
  deleteStreamVoxRole,
  fetchStreamVoxRuntime,
  previewStreamVoxRole,
  updateStreamVoxConfig
} from "./streamvox";
describe("streamvox management api", () => {
  it("读取 StreamVox runtime 状态并校验响应契约", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          model: "qwen3-tts-clone-0.6b-gguf",
          device: "auto",
          llama_backend: null,
          role_name: "demo_role",
          streamvox_seed: null,
          inference_options: {
            language: "chinese",
            icl: false
          },
          sample_rate: 24000,
          roles: ["demo_role", "trial_role"]
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    const runtime = await fetchStreamVoxRuntime("http://127.0.0.1:8765");

    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/streamvox/runtime");
    expect(runtime.roles).toEqual(["demo_role", "trial_role"]);
    expect(runtime.sample_rate).toBe(24000);

    vi.unstubAllGlobals();
  });

  it("提交 StreamVox 配置并删除角色", async () => {
    const runtimePayload = {
      model: "qwen3-tts-clone-1.7b-gguf",
      device: "cpu",
      llama_backend: "vulkan",
      role_name: "demo_role",
      streamvox_seed: 42,
      inference_options: {
        language: "japanese",
        icl: true
      },
      sample_rate: 24000,
      roles: ["demo_role"]
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(runtimePayload), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(runtimePayload), { status: 202 }));

    vi.stubGlobal("fetch", fetchMock);

    await updateStreamVoxConfig("http://127.0.0.1:8765", {
      model: "qwen3-tts-clone-1.7b-gguf",
      device: "cpu",
      llama_backend: "vulkan",
      role_name: "demo_role",
      streamvox_seed: 42,
      inference_options: {
        language: "japanese",
        icl: true
      }
    });
    await deleteStreamVoxRole("http://127.0.0.1:8765", "trial_role");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8765/api/streamvox/config", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        model: "qwen3-tts-clone-1.7b-gguf",
        device: "cpu",
        llama_backend: "vulkan",
        role_name: "demo_role",
        streamvox_seed: 42,
        inference_options: {
          language: "japanese",
          icl: true
        }
      })
    });
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/streamvox/roles/trial_role", {
      method: "DELETE"
    });

    vi.unstubAllGlobals();
  });

  it("创建自定义音色使用 multipart，试听角色返回音频 Blob", async () => {
    const runtimePayload = {
      model: "qwen3-tts-clone-0.6b-gguf",
      device: "auto",
      llama_backend: null,
      role_name: "demo_role",
      streamvox_seed: null,
      inference_options: {
        language: "chinese",
        icl: false
      },
      sample_rate: 24000,
      roles: ["demo_role", "custom_role"]
    };
    const previewBlob = new Blob(["wav"], { type: "audio/wav" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(runtimePayload), { status: 202 }))
      .mockResolvedValueOnce(new Response(previewBlob, { status: 200, headers: { "Content-Type": "audio/wav" } }));

    vi.stubGlobal("fetch", fetchMock);

    const audioFile = new File(["audio"], "reference.wav", { type: "audio/wav" });
    await createStreamVoxRole("http://127.0.0.1:8765", {
      role_name: "custom_role",
      prompt_text: "这是一段参考音频文本。",
      audio_file: audioFile
    });
    const blob = await previewStreamVoxRole("http://127.0.0.1:8765", {
      role_name: "custom_role",
      text: "你好，这是试听。"
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8765/api/streamvox/roles",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData)
      })
    );
    const formData = fetchMock.mock.calls[0][1]?.body as FormData;
    expect(formData.get("role_name")).toBe("custom_role");
    expect(formData.get("prompt_text")).toBe("这是一段参考音频文本。");
    expect(formData.get("audio_file")).toBe(audioFile);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/streamvox/roles/custom_role/preview", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        text: "你好，这是试听。"
      })
    });
    expect(blob.type).toBe("audio/wav");

    vi.unstubAllGlobals();
  });

  it("试听 StreamVox 音色失败时展示后端 detail", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "StreamVox role preview failed: sdk preview failed" }), {
        status: 500,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    vi.stubGlobal("fetch", fetchMock);

    await expect(
      previewStreamVoxRole("http://127.0.0.1:8765", {
        role_name: "欲",
        text: "你好，这是试听。"
      })
    ).rejects.toThrow("试听 StreamVox 音色失败：StreamVox role preview failed: sdk preview failed");

    vi.unstubAllGlobals();
  });

  it("StreamVox runtime 响应缺少 roles 时抛出契约错误", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          model: "qwen3-tts-clone-0.6b-gguf",
          device: "auto",
          role_name: "demo_role",
          sample_rate: 24000
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchStreamVoxRuntime("http://127.0.0.1:8765")).rejects.toThrow(
      "读取 StreamVox 状态失败：响应格式不符合控制台契约"
    );

    vi.unstubAllGlobals();
  });
});

