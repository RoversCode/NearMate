import sharedControlEvents from "../../../../../shared/control-events.json";

export type ControlEventDomain =
  | "application"
  | "interaction"
  | "materials"
  | "turn_admission"
  | "assistant_output"
  | "activity"
  | "reminder"
  | "settings"
  | "audio_devices"
  | "shell";

type EventDefinition = {
  name: string;
  domain: ControlEventDomain;
};

type SharedEventCatalogPayload = {
  ui_to_core: readonly EventDefinition[];
  core_to_ui: readonly EventDefinition[];
};

const UI_TO_CORE_EVENT_DEFINITIONS = [
  { name: "ui.text_submitted", domain: "interaction" },
  { name: "ui.settings_updated", domain: "settings" },
  { name: "ui.avatar_tapped", domain: "interaction" }
] as const satisfies readonly EventDefinition[];

const CORE_TO_UI_EVENT_DEFINITIONS = [
  { name: "runtime.initialization_changed", domain: "application" },
  { name: "bootstrap.status_changed", domain: "application" },
  { name: "followup.state_changed", domain: "interaction" },
  { name: "interaction.state_changed", domain: "interaction" },
  { name: "input.runtime_changed", domain: "interaction" },
  { name: "wake.detected", domain: "interaction" },
  { name: "environment.snapshot_requested", domain: "interaction" },
  { name: "materials.contexts_changed", domain: "materials" },
  { name: "materials.assets_released", domain: "materials" },
  { name: "materials.preflight_failed", domain: "materials" },
  { name: "turn_submissions.snapshot_changed", domain: "turn_admission" },
  { name: "turn.state_changed", domain: "interaction" },
  { name: "input.smart_turn_waiting_user_started", domain: "interaction" },
  { name: "input.smart_turn_waiting_user_ended", domain: "interaction" },
  { name: "user.partial_transcript", domain: "interaction" },
  { name: "user.final_transcript", domain: "interaction" },
  { name: "self_reflection.episode_committed", domain: "interaction" },
  { name: "assistant.text_chunk", domain: "assistant_output" },
  { name: "assistant.text_segment_final", domain: "assistant_output" },
  { name: "assistant.text_final", domain: "assistant_output" },
  { name: "assistant.request_failed", domain: "assistant_output" },
  { name: "assistant.self_reflection_status_changed", domain: "assistant_output" },
  { name: "assistant.output_status_changed", domain: "assistant_output" },
  { name: "assistant.audio_finished", domain: "assistant_output" },
  { name: "assistant.playout_delivery_ready", domain: "assistant_output" },
  { name: "assistant.playout_delivery_failed", domain: "assistant_output" },
  { name: "task_result.started", domain: "activity" },
  { name: "task_result.completed", domain: "activity" },
  { name: "task_result.failed", domain: "activity" },
  { name: "task_result.cancelled", domain: "activity" },
  { name: "background_agent.interactions_snapshot", domain: "activity" },
  { name: "background_agent.interaction_requested", domain: "activity" },
  { name: "background_agent.interaction_submitted", domain: "activity" },
  { name: "background_agent.interaction_resolved", domain: "activity" },
  { name: "activities.snapshot_changed", domain: "activity" },
  { name: "reminders.snapshot_changed", domain: "reminder" },
  { name: "barge_in.detected", domain: "interaction" },
  { name: "settings.changed", domain: "settings" },
  { name: "settings.shortcut_status_changed", domain: "settings" },
  { name: "audio.devices_change_requested", domain: "audio_devices" },
  { name: "audio.devices_apply_result", domain: "audio_devices" },
  { name: "shell.avatar_bubble_changed", domain: "shell" }
] as const satisfies readonly EventDefinition[];

export const CORE_TO_UI_EVENTS = CORE_TO_UI_EVENT_DEFINITIONS.map(
  definition => definition.name
);

export type CoreToUiEventName = (typeof CORE_TO_UI_EVENT_DEFINITIONS)[number]["name"];
type CoreToUiEventDefinition = (typeof CORE_TO_UI_EVENT_DEFINITIONS)[number];

export type CoreToUiEventNameByDomain<Domain extends ControlEventDomain> = Extract<
  CoreToUiEventDefinition,
  { domain: Domain }
>["name"];

validateSharedCatalog(sharedControlEvents as SharedEventCatalogPayload);

/**
 * 同时核对名称、顺序、领域和唯一性，任何漂移都在模块加载时失败。
 */
function validateSharedCatalog(sharedCatalog: SharedEventCatalogPayload): void {
  assertSectionMatches("ui_to_core", UI_TO_CORE_EVENT_DEFINITIONS, sharedCatalog.ui_to_core);
  assertSectionMatches("core_to_ui", CORE_TO_UI_EVENT_DEFINITIONS, sharedCatalog.core_to_ui);
}

/**
 * @param sectionName 共享目录中的分区名称。
 * @param expectedDefinitions Desktop 编译期定义。
 * @param actualDefinitions JSON 运行时定义。
 */
function assertSectionMatches(
  sectionName: string,
  expectedDefinitions: readonly EventDefinition[],
  actualDefinitions: readonly EventDefinition[]
): void {
  if (expectedDefinitions.length !== actualDefinitions.length) {
    throw new Error(`shared 事件目录 ${sectionName} 长度不一致`);
  }

  const seenNames = new Set<string>();
  for (let index = 0; index < expectedDefinitions.length; index += 1) {
    const expected = expectedDefinitions[index];
    const actual = actualDefinitions[index];
    if (!expected || !actual || expected.name !== actual.name || expected.domain !== actual.domain) {
      throw new Error(`shared 事件目录 ${sectionName} 在索引 ${index} 处不一致`);
    }
    if (seenNames.has(actual.name)) {
      throw new Error(`shared 事件目录 ${sectionName} 存在重复事件 ${actual.name}`);
    }
    seenNames.add(actual.name);
  }
}
