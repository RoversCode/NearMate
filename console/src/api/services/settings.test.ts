import { describe, expect, it, vi } from "vitest";

import type { ConsoleSettings } from "../contracts/settings";
import { fetchSettings, updateSettings } from "./settings";

describe("fetchSettings", () => {
  it("从控制台设置接口读取 show_speech_bubbles 开关", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          settings_revision: 0,
          show_speech_bubbles: false,
          active_character_caption_timing: null,
          selected_live2d_model_id: "Haru",
          user_self_intro: "我在开发本地虚拟伴侣。",
          user_extra_notes: "我喜欢直接、清楚的技术讨论。",
          streamvox_device: "auto",
          streamvox_llama_backend: null,
          speaker_verification_enabled: false,
          speaker_whitelist_audio_paths: [],
          followup_toggle_shortcut_enabled: true,
          followup_toggle_shortcut: "CommandOrControl+Alt+Space",
          avatar_visibility_shortcut_enabled: true,
          avatar_visibility_shortcut: "CommandOrControl+Q",
          selection_capture_shortcut_enabled: true,
          selection_capture_shortcut: "CommandOrControl+Alt+C",
          audio_device_mode: "system_default",
          audio_input_device_id: null,
          audio_input_device_name: null,
          audio_output_reference_device_id: null,
          audio_output_reference_device_name: null
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    const settings = await fetchSettings("http://127.0.0.1:8765");

    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/settings");
    expect(settings.settings_revision).toBe(0);
    expect(settings.show_speech_bubbles).toBe(false);
    expect(settings.selected_live2d_model_id).toBe("Haru");
    expect(settings.user_self_intro).toBe("我在开发本地虚拟伴侣。");
    expect(settings.user_extra_notes).toBe("我喜欢直接、清楚的技术讨论。");
    expect(settings.streamvox_device).toBe("auto");
    expect(settings.streamvox_llama_backend).toBeNull();
    expect(settings.speaker_verification_enabled).toBe(false);
    expect(settings.speaker_whitelist_audio_paths).toEqual([]);
    expect(settings.followup_toggle_shortcut_enabled).toBe(true);
    expect(settings.followup_toggle_shortcut).toBe("CommandOrControl+Alt+Space");
    expect(settings.avatar_visibility_shortcut_enabled).toBe(true);
    expect(settings.avatar_visibility_shortcut).toBe("CommandOrControl+Q");
    expect(settings.selection_capture_shortcut_enabled).toBe(true);
    expect(settings.selection_capture_shortcut).toBe("CommandOrControl+Alt+C");
    expect(settings.audio_device_mode).toBe("system_default");
    expect(settings.audio_input_device_id).toBeNull();

    vi.unstubAllGlobals();
  });

  it("拒绝缺少全局设置字段的响应", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          settings_revision: 0,
          show_speech_bubbles: false,
          selected_live2d_model_id: "Haru",
          speaker_verification_enabled: false,
          speaker_whitelist_audio_paths: []
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchSettings("http://127.0.0.1:8765")).rejects.toThrow("响应格式不符合控制台契约");

    vi.unstubAllGlobals();
  });

  it("拒绝只缺少当前角色字幕时序的响应", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          settings_revision: 0,
          show_speech_bubbles: true,
          selected_live2d_model_id: "Haru",
          user_self_intro: "",
          user_extra_notes: "",
          streamvox_device: "auto",
          streamvox_llama_backend: null,
          speaker_verification_enabled: false,
          speaker_whitelist_audio_paths: [],
          followup_toggle_shortcut_enabled: true,
          followup_toggle_shortcut: "CommandOrControl+Alt+Space",
          avatar_visibility_shortcut_enabled: true,
          avatar_visibility_shortcut: "CommandOrControl+Q",
          selection_capture_shortcut_enabled: true,
          selection_capture_shortcut: "CommandOrControl+Alt+C",
          audio_device_mode: "system_default",
          audio_input_device_id: null,
          audio_input_device_name: null,
          audio_output_reference_device_id: null,
          audio_output_reference_device_name: null
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchSettings("http://127.0.0.1:8765")).rejects.toThrow("响应格式不符合控制台契约");

    vi.unstubAllGlobals();
  });

  it("忽略历史角色体验字段，只消费全局设置契约", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          settings_revision: 0,
          show_speech_bubbles: true,
          active_character_caption_timing: null,
          selected_live2d_model_id: "Haru",
          user_self_intro: "",
          user_extra_notes: "",
          streamvox_device: "auto",
          streamvox_llama_backend: null,
          speaker_verification_enabled: false,
          speaker_whitelist_audio_paths: [],
          followup_toggle_shortcut_enabled: true,
          followup_toggle_shortcut: "CommandOrControl+Alt+Space",
          avatar_visibility_shortcut_enabled: true,
          avatar_visibility_shortcut: "CommandOrControl+Q",
          selection_capture_shortcut_enabled: true,
          selection_capture_shortcut: "CommandOrControl+Alt+C",
          audio_device_mode: "system_default",
          audio_input_device_id: null,
          audio_input_device_name: null,
          audio_output_reference_device_id: null,
          audio_output_reference_device_name: null,
          wakeword_keywords: ["旧唤醒词"],
          wake_response: {},
          streamvox_model: "legacy-model",
          streamvox_role_name: "legacy-role",
          streamvox_seed: 7,
          streamvox_inference_options: { mode: 1 }
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    const settings = await fetchSettings("http://127.0.0.1:8765");

    expect(Object.keys(settings)).toEqual([
      "settings_revision",
      "show_speech_bubbles",
      "active_character_caption_timing",
      "selected_live2d_model_id",
      "user_self_intro",
      "user_extra_notes",
      "streamvox_device",
      "streamvox_llama_backend",
      "speaker_verification_enabled",
      "speaker_whitelist_audio_paths",
      "followup_toggle_shortcut_enabled",
      "followup_toggle_shortcut",
      "avatar_visibility_shortcut_enabled",
      "avatar_visibility_shortcut",
      "selection_capture_shortcut_enabled",
      "selection_capture_shortcut",
      "audio_device_mode",
      "audio_input_device_id",
      "audio_input_device_name",
      "audio_output_reference_device_id",
      "audio_output_reference_device_name"
    ]);

    vi.unstubAllGlobals();
  });
});

describe("updateSettings", () => {
  it("只提交普通设置字段并返回后端提交后的权威快照", async () => {
    const settings: ConsoleSettings = {
      settings_revision: 17,
      show_speech_bubbles: true,
      active_character_caption_timing: {
        ms_per_unit: 120,
        calibration_text: "字幕校准文本",
        duration_ms: 1200,
        unit_count: 10,
        measured_at: 1_754_992_800
      },
      selected_live2d_model_id: "Haru",
      user_self_intro: "我在开发本地虚拟伴侣。",
      user_extra_notes: "我喜欢直接、清楚的技术讨论。",
      streamvox_device: "auto",
      streamvox_llama_backend: null,
      speaker_verification_enabled: false,
      speaker_whitelist_audio_paths: [],
      followup_toggle_shortcut_enabled: true,
      followup_toggle_shortcut: "CommandOrControl+Alt+Space",
      avatar_visibility_shortcut_enabled: true,
      avatar_visibility_shortcut: "CommandOrControl+Q",
      selection_capture_shortcut_enabled: true,
      selection_capture_shortcut: "CommandOrControl+Alt+C",
      audio_device_mode: "system_default",
      audio_input_device_id: null,
      audio_input_device_name: null,
      audio_output_reference_device_id: null,
      audio_output_reference_device_name: null
    };
    const committedSettings: ConsoleSettings = {
      ...settings,
      settings_revision: 18
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
      return new Response(JSON.stringify(committedSettings), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const savedSettings = await updateSettings("http://127.0.0.1:8765/", settings);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [requestUrl, requestInit] = fetchMock.mock.calls[0];
    expect(requestUrl).toBe("http://127.0.0.1:8765/api/settings");
    expect(requestInit).toMatchObject({
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      }
    });
    const requestBody = JSON.parse(String(requestInit?.body)) as Record<string, unknown>;
    expect(requestBody).toEqual({
      expected_revision: 17,
      show_speech_bubbles: true,
      selected_live2d_model_id: "Haru",
      user_self_intro: "我在开发本地虚拟伴侣。",
      user_extra_notes: "我喜欢直接、清楚的技术讨论。",
      followup_toggle_shortcut_enabled: true,
      followup_toggle_shortcut: "CommandOrControl+Alt+Space",
      avatar_visibility_shortcut_enabled: true,
      avatar_visibility_shortcut: "CommandOrControl+Q",
      selection_capture_shortcut_enabled: true,
      selection_capture_shortcut: "CommandOrControl+Alt+C"
    });
    expect(requestBody).not.toHaveProperty("settings_revision");
    expect(requestBody).not.toHaveProperty("active_character_caption_timing");
    expect(requestBody).not.toHaveProperty("streamvox_device");
    expect(requestBody).not.toHaveProperty("speaker_verification_enabled");
    expect(requestBody).not.toHaveProperty("audio_device_mode");
    expect(savedSettings).toEqual(committedSettings);

    vi.unstubAllGlobals();
  });

  it("拒绝保存接口返回的非设置快照", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify({ accepted: true }), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      updateSettings("http://127.0.0.1:8765", {
        settings_revision: 0,
        show_speech_bubbles: true,
        active_character_caption_timing: null,
        selected_live2d_model_id: "Haru",
        user_self_intro: "",
        user_extra_notes: "",
        streamvox_device: "auto",
        streamvox_llama_backend: null,
        speaker_verification_enabled: false,
        speaker_whitelist_audio_paths: [],
        followup_toggle_shortcut_enabled: true,
        followup_toggle_shortcut: "CommandOrControl+Alt+Space",
        avatar_visibility_shortcut_enabled: true,
        avatar_visibility_shortcut: "CommandOrControl+Q",
        selection_capture_shortcut_enabled: true,
        selection_capture_shortcut: "CommandOrControl+Alt+C",
        audio_device_mode: "system_default",
        audio_input_device_id: null,
        audio_input_device_name: null,
        audio_output_reference_device_id: null,
        audio_output_reference_device_name: null
      })
    ).rejects.toThrow("保存设置失败：响应格式不符合控制台契约");

    vi.unstubAllGlobals();
  });
});

