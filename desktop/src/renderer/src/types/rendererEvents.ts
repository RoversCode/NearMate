import type { BackendEvent, ConnectionStatus } from "./backendEvents";
import type { ReminderSettings } from "./settingsPayloads";

type UiTextSubmittedEvent = {
  type: "ui.text_submitted";
  payload: {
    text: string;
  };
};

type UiAvatarTappedEvent = {
  type: "ui.avatar_tapped";
  payload: Record<string, never>;
};

type SettingsUpdatedEvent = {
  type: "settings.updated";
  payload: ReminderSettings;
};

type ControlConnectionChangedEvent = {
  type: "control.connection_changed";
  payload: {
    status: ConnectionStatus;
    reason?: string;
  };
};

type LiveKitConnectionChangedEvent = {
  type: "livekit.connection_changed";
  payload: {
    status: ConnectionStatus;
    reason?: string;
  };
};

export type RendererAppEvent =
  | BackendEvent // 后端事件总集合：承接核心业务状态回流，是 reducer 最主要的状态驱动来源。
  | UiTextSubmittedEvent // 用户提交文本事件：表示输入框内容已经确认发送，需要写入会话记录并触发后续处理。
  | UiAvatarTappedEvent // 用户点击头像事件：用于承接头像交互产生的本地 UI 行为，目前主要用于时间线留痕。
  | SettingsUpdatedEvent // 设置更新事件：表示提醒开关等前端配置已经变更，需要同步刷新应用状态。
  | ControlConnectionChangedEvent // 控制链路连接状态变更事件：用于更新与控制层通信通道的连通性展示。
  | LiveKitConnectionChangedEvent; // 媒体链路连接状态变更事件：用于更新实时音视频或媒体通道的连接状态。
