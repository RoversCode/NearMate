import { type CharacterCardPayload, type CharacterCardsPayload } from "../api/contracts/characterCards";
import { type LlmConfig, type SupportedLlmModelsResponse } from "../api/contracts/llm";
import { type LogRow } from "../api/contracts/records";
import { type ConsoleSettings } from "../api/contracts/settings";
import { type ShortcutStatusPayload } from "../api/contracts/shortcutStatus";
import { type StreamVoxRuntimeState } from "../api/contracts/streamvox";
import { type WakeResponseConfig } from "../api/contracts/wakeResponse";

export type ConsoleSectionId = "settings" | "tasks" | "skills" | "usage" | "character-cards" | "streamvox" | "logs";
export type TaskView = "activities" | "reminders";

export type ConsoleSection = {
  id: ConsoleSectionId;
  label: string;
};

export type ConsoleData = {
  settings: ConsoleSettings | null;
  shortcutStatus: ShortcutStatusPayload | null;
  llmConfig: LlmConfig | null;
  supportedLlmModels: SupportedLlmModelsResponse | null;
  characterCards: CharacterCardsPayload | null;
  activeCharacterCard: CharacterCardPayload | null;
  streamVoxRuntime: StreamVoxRuntimeState | null;
  wakeResponseConfig: WakeResponseConfig;
  logs: LogRow[];
};

export type ConsoleLoadState = {
  status: "loading" | "ready" | "failed";
  errorMessage: string | null;
};

export const consoleSections: ConsoleSection[] = [
  {
    id: "settings",
    label: "设置"
  },
  {
    id: "tasks",
    label: "任务"
  },
  {
    id: "skills",
    label: "技能库"
  },
  {
    id: "usage",
    label: "用量统计"
  },
  {
    id: "character-cards",
    label: "角色卡"
  },
  {
    id: "streamvox",
    label: "StreamVox"
  },
  {
    id: "logs",
    label: "运行日志"
  }
];

export const activeSectionDescriptionById: Record<ConsoleSectionId, string> = {
  settings: "管理快捷键、声音、AI 服务和个人资料。",
  tasks: "查看和管理本次运行中的执行任务与定时提醒。",
  skills: "查看并控制后台工作可以使用的 NearMate 产品 Skill。",
  usage: "按日期查看后台工作 Token、缓存命中和 SDK 参考成本。",
  "character-cards": "管理新会话生效的角色人格、羁绊与呈现方式。",
  streamvox: "管理语音模型、推理参数和当前模型下的音色。",
  logs: "查看本次运行的结构化事件，辅助定位运行链路。"
};

export const initialConsoleData: ConsoleData = {
  settings: null,
  shortcutStatus: null,
  llmConfig: null,
  supportedLlmModels: null,
  characterCards: null,
  activeCharacterCard: null,
  streamVoxRuntime: null,
  wakeResponseConfig: {
    buckets: {
      morning: [],
      afternoon: [],
      evening: [],
      night: []
    }
  },
  logs: []
};
