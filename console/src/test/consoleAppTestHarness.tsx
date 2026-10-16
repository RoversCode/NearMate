import { ttsModelsFixture, ttsCapabilitiesFixture, createTtsConfigurationFixture, createTtsTaskFixture } from "./ttsSetupFixtures";
import { fireEvent, screen } from "@testing-library/react";
import { vi } from "vitest";

import type { AudioDeviceTestResult } from "../api/contracts/audioDevices";
import type { BackgroundAgentConfig } from "../api/contracts/backgroundAgent";
import type { ConsoleSettings } from "../api/contracts/settings";
import type { SpeakerVerificationProfile } from "../api/contracts/speakerVerification";
import {
  createCharacterCardPayload,
  createCharacterCardsPayloadWithYaeMiko,
  createYaeMikoCharacterCard
} from "./characterCardFixtures";
import { createSkillLibraryResponse } from "./skillFixtures";

/**
 * 描述控制台 fetch 测试替身的可配置启动状态。
 *
 * 核心字段：isInitialized 控制 /api/bootstrap/status 返回已初始化还是未初始化。
 * 预期用途：复用同一个 fetch mock 构造函数覆盖普通控制台和首次初始化两条路径。
 * 边界异常：字段省略时默认模拟已初始化，避免既有导航测试误进入首次初始化页。
 */
export type ConsoleFetchMockOptions = {
  isInitialized?: boolean;
  includeYaeMikoCard?: boolean;
  backgroundAgentConfig?: ReturnType<typeof createBackgroundAgentConfigPayload>;
  streamVoxRuntime?: ReturnType<typeof createStreamVoxRuntimePayload>;
  streamVoxRuntimeError?: string;
  speakerProfile?: ReturnType<typeof createSpeakerVerificationProfilePayload>;
  audioDeviceTestResult?: AudioDeviceTestResult;
  audioDeviceTestResponse?: Promise<Response>;
  shortcutStatus?: ReturnType<typeof createShortcutStatusPayload>;
};

function createBackgroundAgentConfigPayload(): BackgroundAgentConfig {
  return {
    environment: {
      ANTHROPIC_BASE_URL: "https://endpoint.example.com",
      ANTHROPIC_DEFAULT_OPUS_MODEL: "deepseek-v4-pro[1m]",
      ANTHROPIC_DEFAULT_SONNET_MODEL: "deepseek-v4-pro[1m]",
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "deepseek-flash"
    },
    model: "deepseek-v4-pro[1m]",
    effort_level: null,
    max_turns: 30,
    permission_mode: "acceptEdits",
    credential_type: "api_key",
    credential_configured: true
  };
}

function createBackgroundAgentUsagePayload() {
  const tokenMetrics = {
    non_cached_input_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_input_tokens: 0,
    output_tokens: 0,
    cache_hit_tokens: 0,
    cache_hit_rate: null
  };
  return {
    from_date: "2026-08-02",
    to_date: "2026-08-02",
    summary: {
      ...tokenMetrics,
      task_count: 0,
      complete_task_count: 0,
      incomplete_task_count: 0,
      reference_cost_usd: null,
      reference_cost_task_count: 0
    },
    by_model: [],
    by_day: []
  };
}

export type ConsoleControlWebSocketInstance = {
  close: ReturnType<typeof vi.fn>;
  emitMessage(message: unknown): void;
  emitOpen(): void;
  emitClose(): void;
};

/**
 * 构造控制台设置页测试所需的完整设置快照。
 *
 * 核心入参：无，返回值固定包含提醒开关、模型选择和用户资料三项字段。
 * 预期输出：可直接用于 /api/settings 的 JSON 响应或提交断言。
 * 边界异常：测试场景只关心数据契约，不在这里模拟后端异常。
 */
export function createConsoleSettingsPayload(): ConsoleSettings {
  return {
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
  };
}

function createShortcutStatusPayload() {
  return {
    items: [
      {
        shortcut_id: "followup_toggle",
        accelerator: "CommandOrControl+Alt+Space",
        enabled: true,
        status: "registered",
        settings_revision: 0,
        checked_at: "2026-07-09T14:00:00+08:00",
        message: "快捷键可用"
      },
      {
        shortcut_id: "avatar_visibility",
        accelerator: "CommandOrControl+Q",
        enabled: true,
        status: "registered",
        settings_revision: 0,
        checked_at: "2026-07-09T14:00:00+08:00",
        message: "快捷键可用"
      },
      {
        shortcut_id: "selection_capture",
        accelerator: "CommandOrControl+Alt+C",
        enabled: true,
        status: "registered",
        settings_revision: 0,
        checked_at: "2026-07-09T14:00:00+08:00",
        message: "快捷键可用"
      }
    ]
  };
}

export function createShortcutStatusResponse(): Response {
  return new Response(JSON.stringify(createShortcutStatusPayload()), {
    headers: {
      "Content-Type": "application/json"
    }
  });
}

export function createWakeResponseConfigPayload() {
  return {
    buckets: {
      morning: [],
      afternoon: [],
      evening: [],
      night: []
    }
  };
}

function createToolWaitingPoliciesPayload() {
  return {
    families: [
      {
        family_id: "weather",
        display_name: "天气",
        latency_class: "external_api",
        default_waiting_enabled: true,
        default_waiting_phrases: ["好，我查一下天气。"],
        builtin_policy: { enabled: true, phrases: ["好，我查一下天气。"] },
        user_policy: null,
        effective_policy: { enabled: true, phrases: ["好，我查一下天气。"], source: "builtin" },
        tools: [
          {
            name: "weather_overview",
            display_name: "实时天气",
            family_id: "weather",
            family_display_name: "天气",
            latency_class: "external_api",
            user_override: null,
            effective_policy: { enabled: true, phrases: ["好，我查一下天气。"], source: "builtin" }
          }
        ]
      }
    ],
    user_config: { families: {}, tools: {} }
  };
}

function createSpeakerVerificationProfilePayload(): SpeakerVerificationProfile {
  return {
    enabled: false,
    samples: [],
    max_sample_count: 10,
    required_speech_seconds: 3,
    can_enable: false
  };
}

/**
 * 构造控制台 StreamVox runtime 测试快照。
 *
 * 核心入参无；预期输出是控制台首屏读取的当前模型、设备、角色、采样率和角色列表。
 * 边界异常：测试只关注 UI 契约，不模拟 StreamVox runtime 失败。
 */
export function createBootstrapStatusPayload(isInitialized: boolean, hasQWeatherApiKey = false) {
  return {
    llm: {
      is_ready: isInitialized,
      missing: isInitialized ? [] : ["qwen.base_url", "qwen.api_key"],
      errors: []
    },
    voice: {
      provider: "local_streamvox",
      // 这些旧 UI 用例聚焦 LLM 初始化；语音选择由独立向导覆盖，避免把历史测试耦合到模型下载。
      is_ready: true,
      phase: "ready",
      model_id: "qwen3-tts-clone-0.6b-gguf",
      role_name: "huohua",
      has_confirmed_voice: isInitialized
    },
    has_qweather_api_key: hasQWeatherApiKey,
    is_initialized: isInitialized
  };
}

export function createSupportedLlmModelsPayload() {
  return {
    providers: [
      {
        provider_id: "deepseek",
        display_name: "DeepSeek",
        default_base_url: "https://api.deepseek.com/v1",
        default_base_url_allowed: true,
        endpoint_normalizer: "standard_url"
      },
      {
        provider_id: "qwen",
        display_name: "Qwen",
        default_base_url: "",
        default_base_url_allowed: false,
        endpoint_normalizer: "qwen_workspace"
      }
    ],
    integrations: [
      {
        integration_id: "deepseek:deepseek-flash:chat",
        provider_id: "deepseek",
        model: "deepseek-flash",
        display_name: "DeepSeek V4.1 Flash",
        api_surface: "chat_completions",
        capabilities: ["json_output", "tool_calling", "image_input"]
      },
      {
        integration_id: "deepseek:deepseek-v4-pro:chat",
        provider_id: "deepseek",
        model: "deepseek-v4-pro",
        display_name: "DeepSeek V4 Pro",
        api_surface: "chat_completions",
        capabilities: ["json_output", "tool_calling"]
      },
      {
        integration_id: "qwen:qwen3.6-plus:chat",
        provider_id: "qwen",
        model: "qwen3.6-plus",
        display_name: "Qwen 3.6 Plus",
        api_surface: "chat_completions",
        capabilities: ["image_input", "json_output", "web_extract", "web_search"]
      },
      {
        integration_id: "qwen:qwen3.6-flash:chat",
        provider_id: "qwen",
        model: "qwen3.6-flash",
        display_name: "Qwen 3.6 Flash",
        api_surface: "chat_completions",
        capabilities: ["json_output"]
      }
    ],
    role_contracts: [
      {
        role_id: "foreground",
        display_name: "Foreground",
        description: "处理实时用户轮和系统提醒。",
        required: true,
        default_integration_id: "deepseek:deepseek-flash:chat",
        required_capabilities: ["tool_calling", "image_input"],
        allowed_integration_ids: [
          "deepseek:deepseek-flash:chat"
        ]
      },
      {
        role_id: "summary",
        display_name: "Summary",
        description: "压缩运行期记忆。",
        required: true,
        default_integration_id: "deepseek:deepseek-flash:chat",
        required_capabilities: [],
        allowed_integration_ids: [
          "deepseek:deepseek-flash:chat",
          "deepseek:deepseek-v4-pro:chat"
        ]
      },
      {
        role_id: "cognitive",
        display_name: "Cognitive",
        description: "执行结构化认知任务。",
        required: true,
        default_integration_id: "qwen:qwen3.6-plus:chat",
        required_capabilities: ["json_output"],
        allowed_integration_ids: ["qwen:qwen3.6-plus:chat"]
      },
      {
        role_id: "character_authoring",
        display_name: "Character Authoring",
        description: "生成角色衍生内容。",
        required: false,
        default_integration_id: "deepseek:deepseek-flash:chat",
        required_capabilities: ["json_output"],
        allowed_integration_ids: [
          "deepseek:deepseek-flash:chat",
          "deepseek:deepseek-v4-pro:chat"
        ]
      }
    ]
  };
}

export function createLlmConfigPayload(isReady = true) {
  const qwenMissing = isReady ? [] : ["qwen.base_url", "qwen.api_key"];
  return {
    schema_version: 2,
    credentials: {
      deepseek: {
        base_url: "https://api.deepseek.com/v1",
        api_key_configured: true
      },
      qwen: {
        base_url: isReady ? "https://workspace_123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1" : "",
        api_key_configured: isReady
      }
    },
    role_bindings: {
      foreground: "deepseek:deepseek-flash:chat",
      summary: "deepseek:deepseek-v4-pro:chat",
      cognitive: "qwen:qwen3.6-plus:chat",
      character_authoring: "deepseek:deepseek-flash:chat"
    },
    status: {
      is_ready: isReady,
      missing: qwenMissing,
      errors: [],
      roles: {
        foreground: {
          enabled: true,
          is_ready: true,
          integration_id: "deepseek:deepseek-flash:chat",
          provider_id: "deepseek",
          missing: [],
          errors: []
        },
        summary: {
          enabled: true,
          is_ready: true,
          integration_id: "deepseek:deepseek-v4-pro:chat",
          provider_id: "deepseek",
          missing: [],
          errors: []
        },
        cognitive: {
          enabled: true,
          is_ready: isReady,
          integration_id: "qwen:qwen3.6-plus:chat",
          provider_id: "qwen",
          missing: qwenMissing,
          errors: []
        },
        character_authoring: {
          enabled: true,
          is_ready: true,
          integration_id: "deepseek:deepseek-flash:chat",
          provider_id: "deepseek",
          missing: [],
          errors: []
        }
      }
    }
  };
}

export function createStreamVoxRuntimePayload() {
  return {
    model: "qwen3-tts-clone-0.6b-gguf",
    device: "auto",
    llama_backend: null,
    role_name: "demo_role",
    streamvox_seed: 42,
    inference_options: {
      language: "chinese",
      icl: false
    },
    sample_rate: 24000,
    roles: ["demo_role", "trial_role"]
  };
}

/**
 * 构造控制台页面测试所需的同源接口替身。
 *
 * 核心入参：options 可指定 bootstrap 是否已初始化，内部根据请求路径返回设置对象或空列表。
 * 预期输出：可被 vi.stubGlobal 挂载的 fetch mock，覆盖首屏加载的全部接口。
 * 边界异常：测试只验证导航渲染，因此未知路径也返回空数组，避免无关接口阻塞页面挂载。
 */
export function createConsoleFetchMock(options: ConsoleFetchMockOptions = {}): ReturnType<typeof vi.fn> {
  // 默认返回已初始化状态，确保既有控制台导航测试仍覆盖普通页面路径。
  const isInitialized = options.isInitialized ?? true;
  // StreamVox runtime 默认使用标准快照；个别测试可以覆盖当前角色来验证音色标签语义。
  const streamVoxRuntime = options.streamVoxRuntime ?? createStreamVoxRuntimePayload();
  const speakerProfile = options.speakerProfile ?? createSpeakerVerificationProfilePayload();
  const shortcutStatus = options.shortcutStatus ?? createShortcutStatusPayload();
  const backgroundAgentConfig =
    options.backgroundAgentConfig ?? createBackgroundAgentConfigPayload();
  const characterCardsPayload = options.includeYaeMikoCard
    ? createCharacterCardsPayloadWithYaeMiko()
    : {
        cards: [createCharacterCardPayload().card],
        active_card_id: createCharacterCardPayload().active_card_id,
        shared_world_canon: createCharacterCardPayload().shared_world_canon
      };

  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    // 将 RequestInfo 统一转成字符串，兼容组件未来使用 Request 对象或 URL 对象发起请求。
    const url = input.toString();

    if (url.endsWith("/api/bootstrap/tts/models")) {
      return new Response(JSON.stringify({ provider: "local_streamvox", models: ttsModelsFixture, configured_model: isInitialized ? ttsModelsFixture[0].model_id : null }));
    }
    if (url.endsWith("/api/bootstrap/tts/configuration")) return new Response(JSON.stringify(createTtsConfigurationFixture(isInitialized)));
    if (url.endsWith("/api/bootstrap/tts/probe")) return new Response(JSON.stringify(ttsCapabilitiesFixture));
    if (url.endsWith("/api/bootstrap/tts/status")) return new Response(JSON.stringify(createTtsTaskFixture(isInitialized ? { phase: "ready", is_ready: true, role_name: "trial_role" } : {})));

    // bootstrap 状态是控制台启动的第一道门禁，测试替身必须先给出明确结果。
    if (url.endsWith("/api/bootstrap/status")) {
      return new Response(
        JSON.stringify(createBootstrapStatusPayload(isInitialized)),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/service-keys/qweather")) {
      return new Response(
        JSON.stringify(createBootstrapStatusPayload(isInitialized, true)),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/llm/config")) {
      return new Response(JSON.stringify(createLlmConfigPayload(isInitialized)), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/llm/supported-models")) {
      return new Response(JSON.stringify(createSupportedLlmModelsPayload()), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/background-agent/config")) {
      return new Response(JSON.stringify(backgroundAgentConfig), {
        status: init?.method === "PUT" ? 202 : 200,
        headers: { "Content-Type": "application/json" }
      });
    }

    if (url.includes("/api/background-agent/usage?")) {
      return new Response(JSON.stringify(createBackgroundAgentUsagePayload()), {
        headers: { "Content-Type": "application/json" }
      });
    }

    if (url.endsWith("/api/console/skills")) {
      return new Response(JSON.stringify(createSkillLibraryResponse()), {
        headers: { "Content-Type": "application/json" }
      });
    }

    // 设置页需要真实布尔字段，确保 App 首次加载能通过控制台设置契约校验。
    if (url.endsWith("/api/settings")) {
      const currentSettings = createConsoleSettingsPayload();
      if (init?.method === "POST") {
        const { expected_revision: expectedRevision, ...submittedSettings } = JSON.parse(
          String(init.body)
        ) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
            ...currentSettings,
            ...submittedSettings,
            settings_revision: Number(expectedRevision) + 1
          }),
          {
            status: 202,
            headers: {
              "Content-Type": "application/json"
            }
          }
        );
      }
      return new Response(JSON.stringify(currentSettings), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/settings/shortcut-status")) {
      return new Response(JSON.stringify(shortcutStatus), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/character-cards/card-1/wake-response") || url.endsWith("/api/character-cards/yae-miko/wake-response")) {
      return new Response(JSON.stringify(createWakeResponseConfigPayload()), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/tool-waiting-policies")) {
      return new Response(JSON.stringify(createToolWaitingPoliciesPayload()), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/audio/devices")) {
      return new Response(
        JSON.stringify({
          microphones: [
            {
              id: "mic-k66",
              name: "麦克风 (K66)",
              is_default: true,
              status: "format_incompatible",
              error_type: "AssertionError",
              error_message: ""
            },
            {
              id: "mic-usb",
              name: "麦克风 (USB Camera)",
              is_default: false,
              status: "available",
              error_type: null,
              error_message: null
            }
          ],
          speaker_loopbacks: [
            {
              id: "speaker-default",
              name: "扬声器 (Realtek) loopback",
              is_default: true,
              status: "available",
              error_type: null,
              error_message: null
            }
          ],
          selected: {
            mode: "system_default",
            microphone_id: null,
            microphone_name: null,
            speaker_loopback_id: null,
            speaker_loopback_name: null
          }
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/audio/devices/test")) {
      if (options.audioDeviceTestResponse !== undefined) {
        return options.audioDeviceTestResponse;
      }

      return new Response(
        JSON.stringify(
          options.audioDeviceTestResult ?? {
            status: "available",
            peak_level: 0.2,
            rms_level: 0.05,
            duration_ms: 3000,
            sample_rate: 48000,
            level_status: "good",
            audio_wav_b64: "UklGRgAAAAA=",
            error_type: null,
            error_message: null
          }
        ),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/settings/audio-devices")) {
      const audioSelection = init?.body === undefined
        ? null
        : JSON.parse(String(init.body)) as {
            mode?: "system_default" | "manual";
            microphone_id?: string | null;
            microphone_name?: string | null;
            speaker_loopback_id?: string | null;
            speaker_loopback_name?: string | null;
          };
      return new Response(JSON.stringify({
        accepted: true,
        request_id: "audio-change-1",
        settings: {
          ...createConsoleSettingsPayload(),
          settings_revision: 1,
          audio_device_mode: audioSelection?.mode ?? "system_default",
          audio_input_device_id: audioSelection?.microphone_id ?? null,
          audio_input_device_name: audioSelection?.microphone_name ?? null,
          audio_output_reference_device_id: audioSelection?.speaker_loopback_id ?? null,
          audio_output_reference_device_name: audioSelection?.speaker_loopback_name ?? null
        }
      }), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    // StreamVox 管理区依赖助手子进程 runtime 状态，个别测试会模拟它超时以验证隔离加载。
    if (url.endsWith("/api/streamvox/runtime")) {
      if (options.streamVoxRuntimeError !== undefined) {
        return new Response(JSON.stringify({ detail: options.streamVoxRuntimeError }), {
          status: 504,
          headers: {
            "Content-Type": "application/json"
          }
        });
      }

      return new Response(JSON.stringify(streamVoxRuntime), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/character-cards")) {
      return new Response(JSON.stringify(characterCardsPayload), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/character-cards/active")) {
      return new Response(JSON.stringify(createCharacterCardPayload()), {
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/character-cards/card-1/activate")) {
      return new Response(JSON.stringify({ ...createCharacterCardPayload(), effect_message: "角色卡已激活" }), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/character-cards/card-1/voice-status")) {
      return new Response(
        JSON.stringify({
          voice_match_status: "mismatched",
          runtime: streamVoxRuntime,
          expected: createCharacterCardPayload().card?.meta.voice_binding
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/character-cards/card-1/voice/apply")) {
      return new Response(
        JSON.stringify({
          voice_match_status: "matched",
          runtime: streamVoxRuntime,
          expected: createCharacterCardPayload().card?.meta.voice_binding
        }),
        {
          status: 202,
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/character-cards/yae-miko/voice/apply")) {
      return new Response(
        JSON.stringify({
          voice_match_status: "matched",
          runtime: streamVoxRuntime,
          expected: createYaeMikoCharacterCard().meta.voice_binding
        }),
        {
          status: 202,
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/character-cards/card-1/voice/capture-from-runtime")) {
      return new Response(JSON.stringify({ ...createCharacterCardPayload(), voice_match_status: "matched" }), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.endsWith("/api/character-cards/card-1/voice/reference-audio")) {
      return new Response(
        JSON.stringify({
          reference_audio_path: "voice/reference.wav",
          effect_message: "参考音频已上传，保存当前卡后生效"
        }),
        {
          status: 202,
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    }

    if (url.endsWith("/api/speaker-verification/profile")) {
      return new Response(JSON.stringify(speakerProfile), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    if (url.includes("/api/speaker-verification/samples")) {
      return new Response(JSON.stringify(speakerProfile), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    // StreamVox 管理动作在本测试替身中只需返回最新状态，具体请求体由交互测试单独断言。
    if (
      url.endsWith("/api/streamvox/config") ||
      url.includes("/api/streamvox/roles/") ||
      url.endsWith("/api/streamvox/roles")
    ) {
      return new Response(JSON.stringify(streamVoxRuntime), {
        status: 202,
        headers: {
          "Content-Type": "application/json"
        }
      });
    }

    // 其他五个历史与观测接口在导航测试中只需要成功返回，空列表能覆盖无记录的首装场景。
    return new Response(JSON.stringify([]), {
      headers: {
        "Content-Type": "application/json"
      }
    });
  });
}

/**
 * 安装控制台控制 WebSocket 替身。
 * 核心输出是可主动注入后端控制事件的连接实例列表。
 */
export function installConsoleControlWebSocketMock(): ConsoleControlWebSocketInstance[] {
  const websocketInstances: ConsoleControlWebSocketInstance[] = [];

  class TestControlWebSocket {
    private readonly listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();
    close = vi.fn();

    constructor() {
      websocketInstances.push({
        close: this.close,
        emitMessage: (message: unknown) => {
          const event = new MessageEvent("message", {
            data: JSON.stringify(message)
          });
          for (const listener of this.listeners.get("message") ?? []) {
            listener(event);
          }
        },
        emitOpen: () => {
          const event = new Event("open");
          for (const listener of this.listeners.get("open") ?? []) {
            listener(event as unknown as MessageEvent<string>);
          }
        },
        emitClose: () => {
          const event = new CloseEvent("close");
          for (const listener of this.listeners.get("close") ?? []) {
            listener(event as unknown as MessageEvent<string>);
          }
        }
      });
    }

    addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
      const listeners = this.listeners.get(type) ?? [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    }
  }

  vi.stubGlobal("WebSocket", TestControlWebSocket);
  return websocketInstances;
}

/**
 * 将音频设备表单切到一个确定的手动麦克风配置。
 * 核心用途是让热更新测试只验证“实际变更后提交”，而不是依赖未变化配置也能提交。
 */
export async function chooseManualUsbMicrophone(): Promise<void> {
  const modeSelect = (await screen.findByLabelText("音频设备选择模式")) as HTMLSelectElement;
  fireEvent.change(modeSelect, {
    target: {
      value: "manual"
    }
  });

  const microphoneSelect = screen.getByLabelText("麦克风") as HTMLSelectElement;
  fireEvent.change(microphoneSelect, {
    target: {
      value: "mic-usb"
    }
  });
}


/**
 * 恢复 Console App 测试修改过的全局计时器与浏览器接口。
 */
export function resetConsoleAppTestEnvironment(): void {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
}
