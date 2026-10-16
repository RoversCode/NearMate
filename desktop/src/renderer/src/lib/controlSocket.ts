import type { BackendEvent, ConnectionStatus, ReminderSettings } from "../types/events";
import {
  createControlHttpClient,
  type ControlHttpClient,
  type FetchSettingsOptions
} from "./controlHttpClient";
import {
  ControlWebSocketClient,
  type ControlWebSocketHandlers
} from "./controlWebSocketClient";
import { reportRendererDiagnostic, type RendererDiagnosticBridge } from "./rendererDiagnostics";
import type { PlayoutDeliveryAck } from "../avatar/playoutDeliveryCoordinator";
import type { SettingsSnapshot } from "../types/settingsSnapshot";
import { CONTROL_RECONNECT_INITIAL_DELAY_MS } from "../../../shared/controlReconnectBackoff";

/**
 * 控制网关连接配置只保留 renderer 真正需要的地址信息。
 * 没有传入时统一走本地默认地址。
 */
type ControlSocketConfig = {
  websocketUrl?: string;
  scope?: "runtime" | "status";
  httpBaseUrl?: string;
  connectTimeoutMs?: number;
  reconnectDelayMs?: number;
  random?: () => number;
  diagnosticsBridge?: RendererDiagnosticBridge;
};

/**
 * 事件与连接状态分开回调，并注明局部设置失败，避免误报整个角色已经断线。
 *
 * @property onStatusChange 接收状态、可选说明与可选来源；旧调用方仍可只接收前两项。
 */
type ControlSocketHandlers = Omit<ControlWebSocketHandlers, "onStatusChange"> & {
  onStatusChange(status: ConnectionStatus, reason?: string, source?: "connection" | "settings"): void;
};

// 只比较本次可写意图，字幕校准属于只读投影，不能用于判断人物保存是否仍有效。
const WRITABLE_REMINDER_SETTINGS_KEYS = [
  "show_speech_bubbles", "followup_toggle_shortcut_enabled", "followup_toggle_shortcut",
  "avatar_visibility_shortcut_enabled", "avatar_visibility_shortcut",
  "selection_capture_shortcut_enabled", "selection_capture_shortcut", "selected_live2d_model_id"
] as const;

/**
 * ControlSocket 只负责组合真实控制网关的 HTTP 与 WebSocket 通信。
 * 后端不可用时，调用方会收到失败状态或异常；这里不再生成本地演示事件。
 *
 * @param config 控制网关地址、连接预算和脱敏诊断出口。
 */
export class ControlSocket {
  private readonly diagnosticsBridge?: RendererDiagnosticBridge;
  private readonly httpClient: ControlHttpClient;
  private readonly webSocketClient: ControlWebSocketClient;
  private handlers: ControlSocketHandlers | null = null;
  private latestSettingsSnapshot: SettingsSnapshot | null = null;
  private settingsConnectionGeneration = 0;

  constructor(config: ControlSocketConfig = {}) {
    const websocketUrl = config.websocketUrl ?? "ws://127.0.0.1:8765/ws/control";
    const httpBaseUrl = config.httpBaseUrl ?? "http://127.0.0.1:8765";
    this.diagnosticsBridge = config.diagnosticsBridge;
    this.httpClient = createControlHttpClient(httpBaseUrl, config.diagnosticsBridge);
    this.webSocketClient = new ControlWebSocketClient({
      websocketUrl,
      scope: config.scope,
      connectTimeoutMs: config.connectTimeoutMs ?? 1200,
      reconnectDelayMs: config.reconnectDelayMs ?? CONTROL_RECONNECT_INITIAL_DELAY_MS,
      random: config.random,
      diagnosticsBridge: config.diagnosticsBridge
    });
  }

  /**
   * 建立控制层连接。
   * 后端不可达时只报告断开状态并安排重连，不制造本地演示数据。
   *
   * @param handlers 接收权威事件与连接状态的回调。
   * @returns 关闭本次连接的清理函数。
   */
  connect(handlers: ControlSocketHandlers): () => void {
    this.handlers = handlers;
    this.webSocketClient.connect({
      onEvent: event => this.emitEvent(event),
      onStatusChange: (status, reason) => {
        // 发起连接时即隔离上次 HTTP；首个 connected 不应淘汰本次连接期间已发起的保存。
        if (status === "connecting") {
          this.settingsConnectionGeneration += 1;
          this.latestSettingsSnapshot = null;
        }
        handlers.onStatusChange(status, reason, "connection");
      }
    });
    // Hook 卸载同时关闭 HTTP 结果的投递资格，内层 WebSocket 清理无法处理在途 POST。
    return () => this.disconnect();
  }

  /**
   * 设置读取优先走真实后端，初次显示时允许回退默认设置。
   * 这里故意不依赖 WebSocket 是否已连上，因为 panel/avatar 启动期就需要先做一次 HTTP 设置水合。
   *
   * @param options 成功确认时要求权威快照，并可传入取消信号。
   * @returns 后端投影；权威读取失败时抛错，普通水合失败时返回共享默认值。
   */
  fetchSettings(options?: FetchSettingsOptions): Promise<ReminderSettings> {
    return this.httpClient.fetchSettings(options);
  }

  /**
   * 提交明确编辑的设置字段，并用 Backend 的提交结果更新当前 renderer。
   *
   * @param settings 本次编辑的字段；原有完整 ReminderSettings 调用仍然接受。
   * @returns 提交与权威状态投递完成；读取、提交或版本冲突失败时抛错。
   */
  async updateSettings(settings: Partial<ReminderSettings>): Promise<void> {
    const connectionGeneration = this.settingsConnectionGeneration;
    let superseded = false;
    try {
      await this.httpClient.updateSettings(settings, snapshot => {
        // 过期结果必须失败，Avatar 才能撤回旧模型；静默丢弃事件却返回成功会留下永久 pending。
        if (connectionGeneration !== this.settingsConnectionGeneration) {
          throw new Error("人物设置连接已变化，请重新选择人物。");
        }
        const latest = this.latestSettingsSnapshot;
        if (latest && latest.settings_revision > snapshot.settings_revision) {
          // 别的设置变更仍满足本次窄意图时可以成功；目标已被替换则让 Avatar 回退并解除等待。
          superseded = WRITABLE_REMINDER_SETTINGS_KEYS.some(key => settings[key] !== undefined && latest[key] !== settings[key]);
          if (superseded) throw new Error("人物设置已被其他窗口更新，请重新选择人物。");
          return;
        }
        this.emitEvent({ type: "settings.changed", payload: snapshot });
      });
    } catch (error) {
      /**
       * 设置写回失败时不能把整条真实连接链路误降级成 demo fallback。
       * 这里保留真实连接并提示当前请求失败，由调用方决定是否重试或回滚 UI。
       */
      reportRendererDiagnostic(
        this.diagnosticsBridge,
        "warn",
        "control.settings_update_unreachable"
      );
      // 旧连接或已卸载 Hook 的失败不能污染新连接；仍属本连接的冲突才通知当前页面。
      if (connectionGeneration === this.settingsConnectionGeneration) {
        this.emitStatus("failed", superseded ? "人物设置已被其他窗口更新，请重新选择人物。" : "设置同步接口不可达，请稍后重试。", "settings");
      }
      throw error;
    }
  }

  /**
   * 上报 Desktop 已经真实接管某次助手语音交付的结果。
   * 回执失败只向上抛出，播放协调器会将其记录为诊断而不自动重试。
   */
  reportPlayoutDeliveryAck(ack: PlayoutDeliveryAck): Promise<void> {
    return this.httpClient.reportPlayoutDeliveryAck(ack);
  }

  /**
   * 主动断开连接时，同时清掉所有连接计时器，避免卸载后继续发事件。
   */
  disconnect(): void {
    this.settingsConnectionGeneration += 1;
    this.webSocketClient.disconnect();
    this.handlers = null;
  }

  /** 把 WebSocket 和 HTTP 提交结果放在同一设置版本轴上，防止迟到快照回退界面。 */
  private emitEvent(event: BackendEvent): void {
    if (event.type === "settings.changed") {
      const revision = event.payload.settings_revision;
      if (this.latestSettingsSnapshot !== null && revision < this.latestSettingsSnapshot.settings_revision) return;
      this.latestSettingsSnapshot = event.payload;
    }
    this.handlers?.onEvent(event);
  }

  /**
   * 分发状态并保留错误来源，让旧调用方继续收到设置失败，新提示能只接纳真实连接变化。
   *
   * @param status 需要通知的状态。
   * @param reason 可选的用户错误说明。
   * @param source 本次状态来自连接还是设置保存。
   * @returns 无返回值；通知仍挂载的调用方。
   */
  private emitStatus(status: ConnectionStatus, reason?: string, source: "connection" | "settings" = "connection"): void {
    this.handlers?.onStatusChange(status, reason, source);
  }
}
