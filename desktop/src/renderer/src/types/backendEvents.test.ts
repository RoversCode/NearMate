import { describe, expect, it } from "vitest";
import { CORE_TO_UI_EVENTS } from "./eventCatalog";
import type { InteractionStateSnapshot } from "./interactionBackendEvents";
import {
  parseBackendEvent as parseBackendEventResult,
  type BackendEvent,
  type SettingsChangedPayload
} from "./backendEvents";

const INPUT_RUNTIME_ID = "input-runtime-00000000-0000-4000-8000-000000000001";
const INPUT_RUNTIME_EPOCH = 1;

it("唤醒事件兼容旧载荷，并严格校验用于快捷键恢复的单调时间", () => {
  const event = { type: "wake.detected", payload: {
    input_runtime_id: INPUT_RUNTIME_ID, input_runtime_epoch: INPUT_RUNTIME_EPOCH, session_id: "voice"
  } };
  expect(parseBackendEvent(event)).toEqual(event);
  const timestamped = { ...event, payload: { ...event.payload, detected_at: 100 } };
  expect(parseBackendEvent(timestamped)).toEqual(timestamped);
  for (const detected_at of [-1, "100", Number.NaN, Number.POSITIVE_INFINITY]) {
    expect(parseBackendEvent({ ...event, payload: { ...event.payload, detected_at } })).toBeNull();
  }
});

const SETTINGS_CHANGED_PAYLOAD = {
  settings_revision: 7,
  show_speech_bubbles: true,
  active_character_caption_timing: {
    ms_per_unit: 80.645,
    calibration_text: "我在这里，听见你说话了。先别急，我们一句一句来。",
    duration_ms: 4000,
    unit_count: 49.6,
    measured_at: 1710000000.25
  },
  followup_toggle_shortcut_enabled: true,
  followup_toggle_shortcut: "CommandOrControl+Alt+Space",
  avatar_visibility_shortcut_enabled: true,
  avatar_visibility_shortcut: "CommandOrControl+Q",
  selection_capture_shortcut_enabled: true,
  selection_capture_shortcut: "CommandOrControl+Alt+C",
  selected_live2d_model_id: "Haru",
  user_self_intro: "",
  user_extra_notes: "",
  streamvox_device: "auto",
  streamvox_llama_backend: null,
  speaker_verification_enabled: false,
  speaker_whitelist_audio_paths: [],
  audio_device_mode: "system_default",
  audio_input_device_id: null,
  audio_input_device_name: null,
  audio_output_reference_device_id: null,
  audio_output_reference_device_name: null,
  background_agent: {
    environment: {
      ANTHROPIC_BASE_URL: "https://example.invalid"
    },
    model: "",
    effort_level: null,
    max_turns: 30,
    permission_mode: "acceptEdits",
    skills: {}
  }
} as const satisfies SettingsChangedPayload;

function parseBackendEvent(value: unknown): BackendEvent | null {
  const result = parseBackendEventResult(value);
  return result.ok ? result.event : null;
}

describe("interaction.state_changed", () => {
  const snapshot = {
    input_runtime_id: INPUT_RUNTIME_ID,
    input_runtime_epoch: INPUT_RUNTIME_EPOCH,
    revision: 0,
    followup_state: "followup_cool",
    turn_state: "idle",
    session_id: "session-voice"
  } as const satisfies InteractionStateSnapshot;

  it.each(["idle", "listening", "processing", "speaking"] as const)(
    "保留 %s 轮次状态下的完整冷热快照",
    turnState => {
      for (const followupState of ["followup_cool", "followup_hot"] as const) {
        const event = {
          type: "interaction.state_changed",
          payload: { ...snapshot, followup_state: followupState, turn_state: turnState }
        };
        expect(parseBackendEventResult(event)).toEqual({ ok: true, event });
      }
    }
  );

  it("接收手动切换来源与安全整数上界", () => {
    const event = {
      type: "interaction.state_changed",
      payload: {
        ...snapshot,
        input_runtime_epoch: Number.MAX_SAFE_INTEGER,
        revision: Number.MAX_SAFE_INTEGER,
        source: "manual_toggle"
      }
    };
    expect(parseBackendEventResult(event)).toEqual({ ok: true, event });
  });

  it.each([
    "input_runtime_id", "input_runtime_epoch", "revision",
    "followup_state", "turn_state", "session_id"
  ] as const)("拒绝缺失必填字段 %s", field => {
    const payload: Partial<InteractionStateSnapshot> = { ...snapshot };
    delete payload[field];
    expect(parseBackendEventResult({ type: "interaction.state_changed", payload })).toEqual({
      ok: false,
      reasonCode: "invalid_payload",
      eventType: "interaction.state_changed"
    });
  });

  it.each([
    ["Runtime 身份为空", { input_runtime_id: "" }],
    ["Runtime 身份空白", { input_runtime_id: "  " }],
    ["Runtime 身份没有前缀", { input_runtime_id: "00000000-0000-4000-8000-000000000001" }],
    ["Runtime 身份没有合法 UUID", { input_runtime_id: "input-runtime-invalid" }],
    ["Runtime 身份显式空值", { input_runtime_id: null }],
    ["Runtime 世代为零", { input_runtime_epoch: 0 }],
    ["Runtime 世代为负数", { input_runtime_epoch: -1 }],
    ["Runtime 世代为小数", { input_runtime_epoch: 1.5 }],
    ["Runtime 世代越过安全整数", { input_runtime_epoch: Number.MAX_SAFE_INTEGER + 1 }],
    ["Runtime 世代为字符串", { input_runtime_epoch: "1" }],
    ["Runtime 世代为布尔值", { input_runtime_epoch: true }],
    ["Runtime 世代为无穷大", { input_runtime_epoch: Number.POSITIVE_INFINITY }],
    ["Runtime 世代为 NaN", { input_runtime_epoch: Number.NaN }],
    ["Runtime 世代显式空值", { input_runtime_epoch: null }],
    ["revision 为负数", { revision: -1 }],
    ["revision 为小数", { revision: 0.5 }],
    ["revision 越过安全整数", { revision: Number.MAX_SAFE_INTEGER + 1 }],
    ["revision 为字符串", { revision: "0" }],
    ["revision 为布尔值", { revision: false }],
    ["revision 为无穷大", { revision: Number.POSITIVE_INFINITY }],
    ["revision 为 NaN", { revision: Number.NaN }],
    ["revision 显式空值", { revision: null }],
    ["跟进模式未登记", { followup_state: "hot" }],
    ["跟进模式显式空值", { followup_state: null }],
    ["轮次状态未登记", { turn_state: "finished" }],
    ["轮次状态是数组", { turn_state: ["idle"] }],
    ["轮次状态显式空值", { turn_state: null }],
    ["交互会话为空", { session_id: "" }],
    ["交互会话空白", { session_id: "  " }],
    ["交互会话为数字", { session_id: 1 }],
    ["交互会话显式空值", { session_id: null }],
    ["source 未登记", { source: "wake" }],
    ["source 显式空值", { source: null }],
    ["额外内部字段", { internal_state: "secret" }]
  ] as const)("拒绝非法快照：%s", (_description, invalidFields) => {
    expect(parseBackendEventResult({
      type: "interaction.state_changed",
      payload: { ...snapshot, ...invalidFields }
    })).toEqual({
      ok: false,
      reasonCode: "invalid_payload",
      eventType: "interaction.state_changed"
    });
  });

  it.each([null, true, "invalid", [], 1])("拒绝非对象载荷 %j", payload => {
    expect(parseBackendEvent({ type: "interaction.state_changed", payload })).toBeNull();
  });

  it("继续接收旧跟进模式与轮次状态事件", () => {
    const followupEvent = {
      type: "followup.state_changed",
      payload: { followup_state: "followup_hot", source: "manual_toggle" }
    };
    const turnEvent = {
      type: "turn.state_changed",
      payload: {
        input_runtime_id: INPUT_RUNTIME_ID,
        input_runtime_epoch: INPUT_RUNTIME_EPOCH,
        turn_state: "speaking",
        session_id: "session-voice"
      }
    };
    expect(parseBackendEvent(followupEvent)).toEqual(followupEvent);
    expect(parseBackendEvent(turnEvent)).toEqual(turnEvent);
  });
});

describe("backendEvents", () => {
  it("Self-Reflection 临时状态只接受完整 generation 身份和布尔状态", () => {
    const event = {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-1",
        session_id: "session-reflection",
        generation_id: "generation-reflection-1",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        revision: 1,
        active: true
      }
    };

    expect(parseBackendEvent(event)).toEqual(event);
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, generation_id: "" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, active: "true" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, internal_reasoning: "secret" }
    })).toBeNull();
  });

  it("后端事件解析使用统一事件目录识别合法 core_to_ui 事件", () => {
    expect(CORE_TO_UI_EVENTS).toContain("interaction.state_changed");
    expect(CORE_TO_UI_EVENTS.indexOf("interaction.state_changed")).toBe(
      CORE_TO_UI_EVENTS.indexOf("followup.state_changed") + 1
    );
    expect(CORE_TO_UI_EVENTS).toContain("wake.detected");
    expect(CORE_TO_UI_EVENTS).toContain("environment.snapshot_requested");
    expect(CORE_TO_UI_EVENTS).toContain("materials.contexts_changed");
    expect(CORE_TO_UI_EVENTS).toContain("materials.assets_released");
    expect(CORE_TO_UI_EVENTS).toContain("turn_submissions.snapshot_changed");
    expect(
      parseBackendEvent({
        type: "wake.detected",
        payload: {
          input_runtime_id: INPUT_RUNTIME_ID,
          input_runtime_epoch: INPUT_RUNTIME_EPOCH,
          session_id: "session-voice"
        }
      })?.type
    ).toBe("wake.detected");
    expect(
      parseBackendEvent({
        type: "environment.snapshot_requested",
        payload: {
          input_runtime_id: INPUT_RUNTIME_ID,
          input_runtime_epoch: INPUT_RUNTIME_EPOCH,
          request_id: "material-snapshot-1",
          reason: "voice_turn_started",
          turn_id: "turn-1",
          turn_sequence: 1,
          session_id: "session-voice",
          created_at: "2026-06-26T10:00:00+08:00"
        }
      })?.type
    ).toBe("environment.snapshot_requested");
    expect(
      parseBackendEvent({
        type: "materials.contexts_changed",
        payload: {
          revision: 0,
          turn_material_context: {
            staged_materials: [],
            active: null
          },
          recent_materials: []
        }
      })?.type
    ).toBe("materials.contexts_changed");
    expect(parseBackendEvent({ type: "ui.text_submitted", payload: { text: "不应接收" } })).toBeNull();
  });

  it("Turn 快照转换字段命名，并拒绝损坏的状态与 submission 身份", () => {
    const submissionId = "00000000-0000-4000-8000-000000000201";
    expect(parseBackendEvent({
      type: "turn_submissions.snapshot_changed",
      payload: {
        revision: 2,
        items: [{
          submission_id: submissionId,
          message_text: "等待发送",
          state: "paused",
          created_order: 1,
          error_message: null
        }]
      }
    })).toEqual({
      type: "turn_submissions.snapshot_changed",
      payload: {
        revision: 2,
        items: [{
          submissionId,
          messageText: "等待发送",
          state: "paused",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });
    expect(parseBackendEvent({
      type: "turn_submissions.snapshot_changed",
      payload: {
        revision: 3,
        items: [{
          submission_id: submissionId,
          message_text: "坏状态",
          state: "sending",
          created_order: 1,
          error_message: null
        }]
      }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "user.final_transcript",
      payload: { text: "正式消息", submission_id: "not-a-uuid" }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "turn_submissions.snapshot_changed",
      payload: {
        revision: 3,
        items: [{
          submission_id: submissionId,
          message_text: "携带未登记字段",
          state: "queued",
          created_order: 1,
          error_message: null,
          internal_owner: "main"
        }]
      }
    })).toBeNull();
  });

  it("材料嵌套结构和快捷键状态都拒绝未登记字段", () => {
    expect(parseBackendEvent({
      type: "materials.contexts_changed",
      payload: {
        revision: 1,
        turn_material_context: {
          staged_materials: [],
          active: null,
          internal_generation: 2
        },
        recent_materials: []
      }
    })).toBeNull();

    const shortcutEvent = {
      type: "settings.shortcut_status_changed",
      payload: {
        items: [
          {
            shortcut_id: "followup_toggle",
            accelerator: "Ctrl+Shift+F",
            enabled: true,
            status: "registered",
            settings_revision: 4,
            checked_at: "2026-08-07T15:00:00+08:00",
            message: ""
          },
          {
            shortcut_id: "avatar_visibility",
            accelerator: "Ctrl+Shift+A",
            enabled: false,
            status: "disabled",
            settings_revision: 4,
            checked_at: "2026-08-07T15:00:00+08:00",
            message: ""
          },
          {
            shortcut_id: "selection_capture",
            accelerator: "Ctrl+Shift+S",
            enabled: true,
            status: "registered",
            settings_revision: 4,
            checked_at: "2026-08-07T15:00:00+08:00",
            message: ""
          }
        ]
      }
    };
    expect(parseBackendEvent(shortcutEvent)).toEqual(shortcutEvent);
    expect(parseBackendEvent({
      ...shortcutEvent,
      payload: {
        items: [
          { ...shortcutEvent.payload.items[0], internal_error: "do not expose" },
          ...shortcutEvent.payload.items.slice(1)
        ]
      }
    })).toBeNull();
  });

  it("settings.changed 只接受 Backend 完整固定快照", () => {
    const settingsEvent = {
      type: "settings.changed",
      payload: SETTINGS_CHANGED_PAYLOAD
    };
    const {
      user_extra_notes: _removedUserExtraNotes,
      ...missingFieldPayload
    } = SETTINGS_CHANGED_PAYLOAD;

    expect(parseBackendEvent(settingsEvent)).toEqual(settingsEvent);
    expect(parseBackendEvent({
      ...settingsEvent,
      payload: missingFieldPayload
    })).toBeNull();
    expect(parseBackendEvent({
      ...settingsEvent,
      payload: {
        ...SETTINGS_CHANGED_PAYLOAD,
        internal_config_path: "C:/private/settings.json"
      }
    })).toBeNull();
    expect(parseBackendEvent({
      ...settingsEvent,
      payload: {
        ...SETTINGS_CHANGED_PAYLOAD,
        background_agent: {
          ...SETTINGS_CHANGED_PAYLOAD.background_agent,
          internal_runtime_dir: "C:/private/runtime"
        }
      }
    })).toBeNull();
    expect(parseBackendEvent({
      ...settingsEvent,
      payload: {
        ...SETTINGS_CHANGED_PAYLOAD,
        background_agent: {
          ...SETTINGS_CHANGED_PAYLOAD.background_agent,
          environment: {
            ANTHROPIC_API_KEY: "SECRET_PROVIDER_TOKEN"
          }
        }
      }
    })).toBeNull();
    expect(parseBackendEvent({
      ...settingsEvent,
      payload: {
        ...SETTINGS_CHANGED_PAYLOAD,
        background_agent: {
          ...SETTINGS_CHANGED_PAYLOAD.background_agent,
          skills: { "invalid skill": true }
        }
      }
    })).toBeNull();
  });

  it("settings.changed 接受浮点字幕权重并拒绝非法校准数值", () => {
    const settingsEvent = {
      type: "settings.changed",
      payload: SETTINGS_CHANGED_PAYLOAD
    };
    const validTiming = SETTINGS_CHANGED_PAYLOAD.active_character_caption_timing;
    const invalidTimings = [
      { ...validTiming, ms_per_unit: 0 },
      { ...validTiming, duration_ms: 4000.5 },
      { ...validTiming, unit_count: Number.POSITIVE_INFINITY },
      { ...validTiming, measured_at: Number.NaN }
    ];

    expect(parseBackendEvent(settingsEvent)).toEqual(settingsEvent);
    for (const activeCharacterCaptionTiming of invalidTimings) {
      expect(parseBackendEvent({
        ...settingsEvent,
        payload: {
          ...SETTINGS_CHANGED_PAYLOAD,
          active_character_caption_timing: activeCharacterCaptionTiming
        }
      })).toBeNull();
    }
  });

  it("实时用户转写必须包含非空正文和有效的可选 Turn 身份", () => {
    const event = {
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: INPUT_RUNTIME_ID,
        input_runtime_epoch: INPUT_RUNTIME_EPOCH,
        session_id: "session-voice",
        turn_id: "turn-1",
        turn_sequence: 1,
        text: "  帮我查  "
      }
    };

    expect(parseBackendEvent(event)).toEqual(event);
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, text: "   " }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, turn_id: "" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, session_id: 42 }
    })).toBeNull();
  });

  it("解析失败使用稳定原因码，不回传原始 payload", () => {
    expect(parseBackendEventResult({ type: "wake.detected" })).toEqual({
      ok: false,
      reasonCode: "invalid_envelope",
      eventType: null
    });
    expect(parseBackendEventResult({
      type: "runtime.secret_event",
      payload: { text: "SECRET_PROVIDER_TOKEN" }
    })).toEqual({
      ok: false,
      reasonCode: "unknown_event_type",
      eventType: "runtime.secret_event"
    });
    expect(parseBackendEventResult({
      type: "user.partial_transcript",
      payload: { text: "SECRET_PROVIDER_TOKEN" }
    })).toEqual({
      ok: false,
      reasonCode: "invalid_payload",
      eventType: "user.partial_transcript"
    });
  });

  it("Activity 快照必须完整满足 UUID、状态分区、时间和取消字段约束", () => {
    const activity = {
      activity_id: "01890f3e-7cc2-7d00-8abc-1234567890ab",
      kind: "future_agent",
      type_label: "未来智能体",
      status: "running",
      goal_summary: "整理用户材料",
      failure_summary: null,
      stage_code: "processing",
      stage_label: "分析与处理",
      started_at: "2026-07-18T10:00:00+08:00",
      updated_at: "2026-07-18T10:00:01+08:00",
      finished_at: null,
      can_cancel: true
    };
    const event = {
      type: "activities.snapshot_changed",
      payload: {
        revision: 2,
        runtime_id: "00000000-0000-4000-8000-0000000000aa",
        active: [activity],
        recent: []
      }
    };

    expect(parseBackendEvent(event)).toEqual(event);
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, internal_revision_source: "registry" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [{ ...activity, reasoning: "internal" }]
      }
    })).toBeNull();
    expect(
      parseBackendEvent({
        ...event,
        payload: { ...event.payload, runtime_id: "not-a-uuid" }
      })
    ).toBeNull();
    const failedActivity = {
      ...activity,
      status: "failed",
      failure_summary: "模型服务暂时不可用。",
      finished_at: "2026-07-18T10:00:02+08:00",
      can_cancel: false
    };
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, active: [], recent: [failedActivity] }
    })).not.toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [],
        recent: [{ ...failedActivity, started_at: null }]
      }
    })).not.toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [{ ...activity, started_at: null }]
      }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [],
        recent: [{ ...failedActivity, failure_summary: null }]
      }
    })).toBeNull();
    expect(
      parseBackendEvent({
        ...event,
        payload: {
          ...event.payload,
          active: [{ ...activity, activity_id: "not-a-uuid" }]
        }
      })
    ).toBeNull();
    expect(
      parseBackendEvent({
        ...event,
        payload: {
          ...event.payload,
          active: [{ ...activity, stage_code: "reasoning" }]
        }
      })
    ).toBeNull();
    expect(
      parseBackendEvent({
        ...event,
        payload: {
          ...event.payload,
          active: [{ ...activity, stage_code: null, stage_label: "分析与处理" }]
        }
      })
    ).toBeNull();
    expect(
      parseBackendEvent({
        ...event,
        payload: {
          ...event.payload,
          active: [{ ...activity, status: "completed" }]
        }
      })
    ).toBeNull();
    expect(
      parseBackendEvent({
        ...event,
        payload: {
          revision: 3,
          runtime_id: "00000000-0000-4000-8000-0000000000aa",
          active: [],
          recent: [{
            ...activity,
            status: "cancelled",
            finished_at: "2026-07-18T10:00:02+08:00",
            can_cancel: true
          }]
        }
      })
    ).toBeNull();
  });

  it("任务结果四类事件必须严格匹配公开字段契约", () => {
    const base = {
      activity_id: "taskResult-a",
      interaction_session_id: "session-a",
      goal: "总结 https://example.com/report",
      created_at: "2026-07-22T10:00:00+08:00"
    };
    const failure = {
      stage_code: "processing",
      stage_label: "分析与处理",
      code: "agent_cli_crash",
      summary: "Claude Code 运行时意外中断，后台任务未能完成。",
      technical_detail: "",
      suggested_action: "请重新发起任务；若反复出现，请把任务编号提供给开发者排查。"
    };
    const events = [
      { type: "task_result.started", payload: base },
      {
        type: "task_result.completed",
        payload: {
          ...base,
          display_content: "完整正文",
          source_materials: [],
          output_paths: ["G:\\NearMate Workspace\\report.md"]
        }
      },
      {
        type: "task_result.failed",
        payload: { ...base, failure, source_materials: [] }
      },
      {
        type: "task_result.cancelled",
        payload: { ...base, message: "已停止生成", reason: "user_cancelled", source_materials: [] }
      }
    ];

    for (const event of events) {
      expect(parseBackendEvent(event)).toEqual(event);
    }
    expect(parseBackendEvent({
      type: "task_result.completed",
      payload: { ...base, display_content: "正文", source_materials: "report.pdf" }
    })).toBeNull();
    // 旧 message/reason 失败形状与多余字段都必须拒收。
    expect(parseBackendEvent({
      type: "task_result.failed",
      payload: { ...base, message: "失败", reason: "exception", source_materials: [] }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "task_result.failed",
      payload: { ...base, failure, source_materials: [], title: "旧字段" }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "task_result.failed",
      payload: { ...base, failure: { ...failure, code: "sdk_internal_error" }, source_materials: [] }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "task_result.failed",
      payload: { ...base, failure: { ...failure, stage_code: "reasoning" }, source_materials: [] }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "task_result.started",
      payload: { ...base, goal: "" }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "task_result.started",
      payload: { ...base, activity_id: undefined, task_id: "legacy-task" }
    })).toBeNull();
  });

  it("助手请求失败事件必须严格校验请求编号、文案和精确字段", () => {
    const event = {
      type: "assistant.request_failed",
      payload: {
        request_id: "text-input-1",
        code: "request_start_failed",
        message: "消息已经发送，但我现在没能开始回复，请稍后再试。"
      }
    };

    expect(parseBackendEvent(event)).toEqual(event);
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, request_id: "" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, extra: true }
    })).toBeNull();
  });

  it("运行时兜底 Final 保留 Notice 失败元数据且允许协议失败空终态", () => {
    const fallbackEvent = {
      type: "assistant.text_final",
      payload: {
        request_id: "request-a",
        session_id: "session-a",
        generation_id: "generation-a",
        text: "这次回复暂时无法继续，请稍后再试。",
        notice_delivery_failed: true as const
      }
    };
    const protocolFailureEvent = {
      type: "assistant.text_final",
      payload: {
        request_id: "request-a",
        session_id: "session-a",
        generation_id: "generation-a",
        text: "",
        notice_delivery_failed: true as const,
        notice_failure_reason: "provider_protocol_leak"
      }
    };

    expect(parseBackendEvent(fallbackEvent)).toEqual(fallbackEvent);
    expect(parseBackendEvent(protocolFailureEvent)).toEqual(protocolFailureEvent);
    expect(parseBackendEvent({
      type: "assistant.text_final",
      payload: { request_id: "request-a", text: "" }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "assistant.text_final",
      payload: {
        request_id: "request-a",
        text: "  ",
        notice_delivery_failed: true
      }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "assistant.text_final",
      payload: {
        request_id: "request-a",
        text: "",
        notice_delivery_failed: true,
        notice_failure_reason: "unknown_reason"
      }
    })).toBeNull();
    expect(parseBackendEvent({
      ...protocolFailureEvent,
      payload: {
        ...protocolFailureEvent.payload,
        notice_delivery_failed: undefined,
        notice_failure_reason: "provider_protocol_leak"
      }
    })).toBeNull();
    expect(parseBackendEvent({
      type: "assistant.text_chunk",
      payload: protocolFailureEvent.payload
    })).toBeNull();
  });

  it("后台待答事件必须使用标准编号、对象载荷和精确字段", () => {
    const requested = {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "123e4567-e89b-42d3-a456-426614174000",
        activity_id: "123e4567-e89b-42d3-a456-426614174001",
        interaction_session_id: "session-a",
        kind: "question",
        tool_name: "AskUserQuestion",
        input_data: {},
        questions: [{
          question: "请选择输出格式",
          header: "格式",
          options: [],
          multiSelect: false
        }]
      }
    };
    const resolved = {
      type: "background_agent.interaction_resolved",
      payload: {
        request_id: requested.payload.request_id,
        activity_id: requested.payload.activity_id,
        outcome: "answered"
      }
    };

    expect(parseBackendEvent(requested)).toEqual(requested);
    expect(parseBackendEvent(resolved)).toEqual(resolved);
    expect(parseBackendEvent({
      ...requested,
      payload: { ...requested.payload, input_data: [] }
    })).toBeNull();
    expect(parseBackendEvent({
      ...requested,
      payload: { ...requested.payload, questions: [null] }
    })).toBeNull();
    expect(parseBackendEvent({
      ...requested,
      payload: { ...requested.payload, questions: [{}] }
    })).toBeNull();
    expect(parseBackendEvent({
      ...requested,
      payload: { ...requested.payload, request_id: "request-a" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...requested,
      payload: { ...requested.payload, extra: true }
    })).toBeNull();
    expect(parseBackendEvent({
      ...resolved,
      payload: { ...resolved.payload, extra: true }
    })).toBeNull();

    const permissionRequested = {
      ...requested,
      payload: {
        ...requested.payload,
        kind: "permission",
        tool_name: "Bash",
        questions: [],
        action_summary: "运行完成当前任务所需的命令",
        grant_scope: "activity"
      }
    };
    expect(parseBackendEvent(permissionRequested)).toEqual(permissionRequested);
    expect(parseBackendEvent({
      ...permissionRequested,
      payload: { ...permissionRequested.payload, action_summary: "" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...permissionRequested,
      payload: { ...permissionRequested.payload, grant_scope: "tool" }
    })).toBeNull();
    expect(parseBackendEvent({
      ...requested,
      payload: { ...requested.payload, action_summary: "不应出现在资料问题里" }
    })).toBeNull();
  });

  it("后台待答完整快照必须严格校验每一张可操作卡片", () => {
    const snapshot = {
      type: "background_agent.interactions_snapshot",
      payload: {
        items: [{
          request_id: "123e4567-e89b-42d3-a456-426614174000",
          activity_id: "123e4567-e89b-42d3-a456-426614174001",
          interaction_session_id: "session-a",
          status: "processing",
          kind: "question",
          tool_name: "AskUserQuestion",
          input_data: {},
          questions: [{
            question: "请选择输出格式",
            header: "格式",
            options: [],
            multiSelect: true
          }]
        }]
      }
    };

    expect(parseBackendEvent(snapshot)).toEqual(snapshot);
    expect(parseBackendEvent({
      ...snapshot,
      payload: { ...snapshot.payload, extra: true }
    })).toBeNull();
    expect(parseBackendEvent({
      ...snapshot,
      payload: {
        items: [{ ...snapshot.payload.items[0], questions: [{ question: "缺少字段" }] }]
      }
    })).toBeNull();
  });

  it("定时提醒快照严格校验运行身份、容量和生命周期分区", () => {
    const scheduled = {
      reminder_id: "123e4567-e89b-42d3-a456-426614174010",
      content: "准备下午的面试",
      state: "scheduled",
      trigger_at: "2026-09-04T15:00:00+08:00",
      created_at: "2026-09-04T10:00:00+08:00",
      updated_at: "2026-09-04T10:00:00+08:00",
      finished_at: null,
      ring_count: 0,
      next_ring_at: null,
      finish_reason: null,
      can_modify: true,
      can_cancel: true,
      can_dismiss: false
    };
    const event = {
      type: "reminders.snapshot_changed",
      payload: {
        runtime_id: "00000000-0000-4000-8000-0000000000cc",
        revision: 1,
        active: [scheduled],
        recent: []
      }
    };

    expect(parseBackendEvent(event)).toEqual(event);
    expect(CORE_TO_UI_EVENTS).toContain("reminders.snapshot_changed");
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, active: [{ ...scheduled, internal_retry_count: 2 }] }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, active: [{ ...scheduled, state: "delivered" }] }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, active: [{ ...scheduled, can_modify: false }] }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: { ...event.payload, active: Array.from({ length: 6 }, () => scheduled) }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [{ ...scheduled, content: "😀".repeat(500) }]
      }
    })).not.toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [{ ...scheduled, content: "😀".repeat(501) }]
      }
    })).toBeNull();
    expect(parseBackendEvent({
      ...event,
      payload: {
        ...event.payload,
        active: [{ ...scheduled, trigger_at: "2026-09-04T15:00:00" }]
      }
    })).toBeNull();
  });
});
