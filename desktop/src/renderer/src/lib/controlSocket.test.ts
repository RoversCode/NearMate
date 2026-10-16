import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ControlSocket } from "./controlSocket";
import type { BackendEvent, ConnectionStatus, ReminderSettings } from "../types/events";
import type { SettingsSnapshot } from "../types/settingsSnapshot";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const originalWebSocket = globalThis.WebSocket;
const backendSettingsFixture: ReminderSettings = {
  show_speech_bubbles: false,
  active_character_caption_timing: null,
  followup_toggle_shortcut_enabled: true,
  followup_toggle_shortcut: "CommandOrControl+Alt+Space",
  avatar_visibility_shortcut_enabled: true,
  avatar_visibility_shortcut: "CommandOrControl+Alt+P",
  selection_capture_shortcut_enabled: true,
  selection_capture_shortcut: "CommandOrControl+Alt+C",
  selected_live2d_model_id: "Natori"
};
const backendSettingsSnapshotFixture: SettingsSnapshot = {
  settings_revision: 3,
  show_speech_bubbles: false,
  active_character_caption_timing: null,
  followup_toggle_shortcut_enabled: true,
  followup_toggle_shortcut: "CommandOrControl+Alt+Space",
  avatar_visibility_shortcut_enabled: true,
  avatar_visibility_shortcut: "CommandOrControl+Alt+P",
  selection_capture_shortcut_enabled: true,
  selection_capture_shortcut: "CommandOrControl+Alt+C",
  selected_live2d_model_id: "Natori",
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
    environment: {},
    model: "",
    effort_level: null,
    max_turns: 30,
    permission_mode: "acceptEdits",
    skills: {}
  }
};
const sharedFallbackSettingsFixture: ReminderSettings = {
  show_speech_bubbles: true,
  followup_toggle_shortcut_enabled: true,
  followup_toggle_shortcut: "CommandOrControl+Alt+Space",
  avatar_visibility_shortcut_enabled: true,
  avatar_visibility_shortcut: "CommandOrControl+Alt+P",
  selected_live2d_model_id: "Haru"
};

/**
 * 测试里显式伪造一个“已连上真实控制层”的 socket 实例，
 * 这样可以只验证 HTTP 设置链路，不把注意力分散到 WebSocket 建连过程。
 */
function createConnectedControlSocket() {
  const events: BackendEvent[] = [];
  const statuses: ConnectionStatus[] = [];
  const controlSocket = new ControlSocket({ connectTimeoutMs: 50 });
  const connectedControlSocket = controlSocket as unknown as {
    socket: { readyState: number };
    handlers: {
      onEvent(event: BackendEvent): void;
      onStatusChange(status: ConnectionStatus): void;
    };
  };
  connectedControlSocket.socket = { readyState: WebSocket.OPEN };
  connectedControlSocket.handlers = {
    onEvent(event) {
      events.push(event);
    },
    onStatusChange(status) {
      statuses.push(status);
    }
  };

  return {
    controlSocket,
    events,
    statuses
  };
}

/**
 * 给 node 测试环境补齐 WebSocket 重连状态机需要的 window 计时器。
 * 核心输出是最小可用的 window 对象，避免控制链路测试被环境缺口抢先打断。
 */
function installWindowTimerShim(): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      clearTimeout: globalThis.clearTimeout,
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
      setTimeout: globalThis.setTimeout
    }
  });
}

/**
 * FakeWebSocket 用于精确控制建连、断连与重连时序。
 * 核心输出是让测试手动触发 open/error/close，不接入真实网络。
 */
class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static readonly instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: unknown }) => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  /**
   * 模拟底层连接成功。
   * 业务意图是让测试验证首次连接和重连完成后的状态回调。
   */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  /**
   * 模拟底层连接异常。
   * 业务意图是覆盖首连失败和已连接后异常两种状态机分支。
   */
  error(): void {
    this.onerror?.();
  }

  /**
   * 模拟底层连接关闭。
   * 业务意图是验证 backend 重启导致的 socket close 后，会自动安排下一轮重连。
   */
  closeFromServer(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  /**
   * 模拟前端主动断开。
   * 业务意图是让 ControlSocket.disconnect() 能复用同一替身而不触发真实网络副作用。
   */
  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  /** 向 renderer 投递一条 JSON 控制事件。 */
  message(event: object): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  /** 向 renderer 投递未经 JSON 包装的原始传输帧。 */
  rawMessage(data: unknown): void {
    this.onmessage?.({ data });
  }
}

describe("ControlSocket", () => {
  it.each([
    ["HTTP 503", () => Promise.resolve(new Response(null, { status: 503 }))],
    ["非法 JSON", () => Promise.resolve(new Response("invalid json"))],
    ["非法设置结构", () => Promise.resolve(new Response(JSON.stringify({ selected_live2d_model_id: "Haru" })))]
  ])("权威设置读取遇到%s时显式失败，禁止默认值伪造成功", async (_name, fetchResponse) => {
    globalThis.fetch = vi.fn(fetchResponse);
    const controlSocket = new ControlSocket();
    await expect(controlSocket.fetchSettings({ requireAuthoritative: true })).rejects.toThrow();
  });
  beforeEach(() => {
    FakeWebSocket.instances.length = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    globalThis.fetch = originalFetch;
    globalThis.WebSocket = originalWebSocket;

    /**
     * 部分用例会安装最小 window shim 来覆盖误入 fallback 的路径。
     * 每个测试结束后恢复原始描述符，避免影响其它 node 环境测试。
     */
    if (originalWindowDescriptor) {
      Object.defineProperty(globalThis, "window", originalWindowDescriptor);
      return;
    }

    delete (globalThis as unknown as { window?: Window }).window;
  });

  it("真实连接下读取设置时，会从 /api/settings 拿到普通设置快照", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(backendSettingsSnapshotFixture), {
          status: 200,
          headers: {
            "Content-Type": "application/json"
          }
        })
      );

    const { controlSocket } = createConnectedControlSocket();

    await expect(controlSocket.fetchSettings()).resolves.toEqual(backendSettingsFixture);
    expect(globalThis.fetch).toHaveBeenCalledWith("http://127.0.0.1:8765/api/settings");
  });

  it("真实连接下读取设置遇到网络失败时，会回退到共享默认设置但不切换到 fallback", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("settings endpoint down"));

    const { controlSocket, statuses } = createConnectedControlSocket();

    await expect(controlSocket.fetchSettings()).resolves.toEqual(sharedFallbackSettingsFixture);
    expect(statuses).toEqual([]);
  });

  it("真实连接下读取设置遇到非 2xx 时，会回退到共享默认设置", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));

    const { controlSocket, statuses } = createConnectedControlSocket();

    await expect(controlSocket.fetchSettings()).resolves.toEqual(sharedFallbackSettingsFixture);
    expect(statuses).toEqual([]);
  });

  it("真实连接下读取设置拿到坏返回体时，会回退到共享默认设置", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response("{", {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );

    const { controlSocket, statuses } = createConnectedControlSocket();

    await expect(controlSocket.fetchSettings()).resolves.toEqual(sharedFallbackSettingsFixture);
    expect(statuses).toEqual([]);
  });

  it.each([
    ["空对象", {}],
    ["缺少字段", (({ settings_revision: _removed, ...rest }) => rest)(backendSettingsSnapshotFixture)],
    ["未知字段", { ...backendSettingsSnapshotFixture, unexpected: true }],
    ["嵌套字段错误", {
      ...backendSettingsSnapshotFixture,
      background_agent: {
        ...backendSettingsSnapshotFixture.background_agent,
        max_turns: "30"
      }
    }]
  ])("HTTP 200 返回%s时回退默认设置并产生脱敏诊断", async (_label, responseBody) => {
    const reportDiagnostic = vi.fn();
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: {
          "Content-Type": "application/json"
        }
      })
    );
    const controlSocket = new ControlSocket({
      diagnosticsBridge: { reportDiagnostic }
    });

    await expect(controlSocket.fetchSettings()).resolves.toEqual(sharedFallbackSettingsFixture);
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "control.settings_snapshot_rejected",
      { reason_code: "invalid_settings_snapshot" }
    );
    expect(JSON.stringify(reportDiagnostic.mock.calls)).not.toContain(JSON.stringify(responseBody));
  });

  it("切换人物提交真实版本与可写字段，保留用户简介和选择快捷键", async () => {
    const authoritative = { ...backendSettingsSnapshotFixture, user_self_intro: "保留简介", user_extra_notes: "保留说明",
      selection_capture_shortcut: "CommandOrControl+Alt+X", selection_capture_shortcut_enabled: false };
    globalThis.fetch = vi.fn(async (_url, init) => {
      if (init?.method !== "POST") return Response.json(authoritative);
      const body = JSON.parse(String(init.body));
      if (body.expected_revision !== authoritative.settings_revision || "active_character_caption_timing" in body) {
        return Response.json({ detail: "请求契约不匹配" }, { status: 422 });
      }
      return Response.json({ ...authoritative, selected_live2d_model_id: body.selected_live2d_model_id,
        settings_revision: authoritative.settings_revision + 1 }, { status: 202 });
    });

    const { controlSocket, events } = createConnectedControlSocket();
    const nextSettings = { selected_live2d_model_id: "Rice" };

    await expect(controlSocket.updateSettings(nextSettings)).resolves.toBeUndefined();
    expect(globalThis.fetch).toHaveBeenCalledWith("http://127.0.0.1:8765/api/settings", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ expected_revision: authoritative.settings_revision,
        show_speech_bubbles: authoritative.show_speech_bubbles,
        followup_toggle_shortcut_enabled: authoritative.followup_toggle_shortcut_enabled,
        followup_toggle_shortcut: authoritative.followup_toggle_shortcut,
        avatar_visibility_shortcut_enabled: authoritative.avatar_visibility_shortcut_enabled,
        avatar_visibility_shortcut: authoritative.avatar_visibility_shortcut,
        selection_capture_shortcut_enabled: false, selection_capture_shortcut: "CommandOrControl+Alt+X",
        selected_live2d_model_id: "Rice", user_self_intro: "保留简介", user_extra_notes: "保留说明" })
    });
    expect(events).toEqual([{ type: "settings.changed", payload: {
      ...authoritative, selected_live2d_model_id: "Rice", settings_revision: 4
    } }]);
  });

  it("真实连接下设置写回遇到非 2xx 时，会显式抛出失败错误", async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockResolvedValueOnce(new Response(null, { status: 503 }));
    const consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const { controlSocket } = createConnectedControlSocket();

    await expect(controlSocket.updateSettings(backendSettingsFixture)).rejects.toThrow(
      "settings update request failed: 503"
    );
    expect(consoleWarnSpy).toHaveBeenCalledWith("control.settings_update_unreachable");
  });

  it.each([
    ["HTTP 503", () => new Response(null, { status: 503 })],
    ["坏 JSON", () => new Response("invalid json")],
    ["残缺快照", () => Response.json({ selected_live2d_model_id: "Haru" })]
  ])("人物写入前权威读取遇到%s时拒绝提交，默认值不能覆盖用户设置", async (_name, response) => {
    globalThis.fetch = vi.fn().mockResolvedValue(response());
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { controlSocket, events } = createConnectedControlSocket();
    await expect(controlSocket.updateSettings({ selected_live2d_model_id: "Rice" })).rejects.toThrow();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(events).toEqual([]);
  });

  it("设置版本冲突409直接失败，不重放旧快照或发布本地成功状态", async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockResolvedValueOnce(Response.json({ detail: "版本已变化" }, { status: 409 }));
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const { controlSocket, events } = createConnectedControlSocket();
    await expect(controlSocket.updateSettings({ selected_live2d_model_id: "Rice" })).rejects.toThrow("settings update request failed: 409");
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    expect(events).toEqual([]);
  });

  it("旧完整ReminderSettings调用仍接受，并排除只读字幕字段", async () => {
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockResolvedValueOnce(Response.json({ ...backendSettingsSnapshotFixture, settings_revision: 4 }));
    const { controlSocket } = createConnectedControlSocket();
    await expect(controlSocket.updateSettings(backendSettingsFixture)).resolves.toBeUndefined();
    const request = vi.mocked(globalThis.fetch).mock.calls[1]?.[1];
    const submitted = JSON.parse(String(request?.body));
    expect(submitted.expected_revision).toBe(3);
    expect(submitted.selected_live2d_model_id).toBe("Natori");
    expect(submitted).not.toHaveProperty("active_character_caption_timing");
  });

  it("WebSocket新版本已替换人物时，迟到HTTP必须失败且不能回退界面", async () => {
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    let resolveCommit: (response: Response) => void = () => undefined;
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveCommit = resolve; }));
    const events: BackendEvent[] = [];
    const controlSocket = new ControlSocket();
    const dispose = controlSocket.connect({ onEvent: event => events.push(event), onStatusChange() {} });
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    const submission = controlSocket.updateSettings({ selected_live2d_model_id: "Rice" });
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    const newerSnapshot = { ...backendSettingsSnapshotFixture, settings_revision: 5, selected_live2d_model_id: "Hiyori" };
    socket.message({ type: "settings.changed", payload: newerSnapshot });
    resolveCommit(Response.json({ ...backendSettingsSnapshotFixture, settings_revision: 4, selected_live2d_model_id: "Rice" }));
    await expect(submission).rejects.toThrow("人物设置已被其他窗口更新");
    expect(events).toEqual([{ type: "settings.changed", payload: newerSnapshot }]);
    socket.message({ type: "settings.changed", payload: backendSettingsSnapshotFixture });
    expect(events).toHaveLength(1);
    dispose();
  });

  it("首次WebSocket在HTTP保存期间连上时，仍接纳本次保存的权威结果", async () => {
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    let resolveCommit: (response: Response) => void = () => undefined;
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveCommit = resolve; }));
    const events: BackendEvent[] = [];
    const controlSocket = new ControlSocket();
    const dispose = controlSocket.connect({ onEvent: event => events.push(event), onStatusChange() {} });
    const submission = controlSocket.updateSettings({ selected_live2d_model_id: "Rice" });
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    socket.message({ type: "settings.changed", payload: backendSettingsSnapshotFixture });
    const committed = { ...backendSettingsSnapshotFixture, settings_revision: 4, selected_live2d_model_id: "Rice" };
    resolveCommit(Response.json(committed));
    await submission;
    expect(events.at(-1)).toEqual({ type: "settings.changed", payload: committed });
    dispose();
  });

  it("WebSocket重连后重新接纳低版本，并拒绝上一连接的迟到HTTP结果", async () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    let resolveCommit: (response: Response) => void = () => undefined;
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveCommit = resolve; }));
    const events: BackendEvent[] = [];
    const controlSocket = new ControlSocket({ reconnectDelayMs: 100, random: () => 0.5 });
    const statuses: ConnectionStatus[] = [];
    const dispose = controlSocket.connect({ onEvent: event => events.push(event), onStatusChange: status => statuses.push(status) });
    const first = FakeWebSocket.instances[0]!;
    first.open();
    first.message({ type: "settings.changed", payload: { ...backendSettingsSnapshotFixture, settings_revision: 5 } });
    const submission = controlSocket.updateSettings({ selected_live2d_model_id: "Rice" });
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    first.closeFromServer();
    await vi.advanceTimersByTimeAsync(100);
    const second = FakeWebSocket.instances[1]!;
    second.open();
    const restarted = { ...backendSettingsSnapshotFixture, settings_revision: 0, selected_live2d_model_id: "Haru" };
    second.message({ type: "settings.changed", payload: restarted });
    resolveCommit(Response.json({ ...backendSettingsSnapshotFixture, settings_revision: 6, selected_live2d_model_id: "Rice" }));
    await expect(submission).rejects.toThrow("人物设置连接已变化");
    expect(events.at(-1)).toEqual({ type: "settings.changed", payload: restarted });
    expect(events).toHaveLength(2);
    expect(statuses).not.toContain("failed");
    dispose();
  });

  it("HTTP提交先于首次WebSocket连上时，旧首帧不能回退已提交人物", async () => {
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const committed = { ...backendSettingsSnapshotFixture, settings_revision: 4, selected_live2d_model_id: "Rice" };
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockResolvedValueOnce(Response.json(committed));
    const events: BackendEvent[] = [];
    const controlSocket = new ControlSocket();
    const dispose = controlSocket.connect({ onEvent: event => events.push(event), onStatusChange() {} });
    await controlSocket.updateSettings({ selected_live2d_model_id: "Rice" });
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    socket.message({ type: "settings.changed", payload: backendSettingsSnapshotFixture });
    expect(events).toEqual([{ type: "settings.changed", payload: committed }]);
    dispose();
  });

  it("连接清理期间HTTP仍在保存时，迟到结果不能投递给已卸载的Hook", async () => {
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    let resolveCommit: (response: Response) => void = () => undefined;
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveCommit = resolve; }));
    const events: BackendEvent[] = [];
    const controlSocket = new ControlSocket();
    const statuses: ConnectionStatus[] = [];
    const dispose = controlSocket.connect({ onEvent: event => events.push(event), onStatusChange: status => statuses.push(status) });
    FakeWebSocket.instances[0]!.open();
    const submission = controlSocket.updateSettings({ selected_live2d_model_id: "Rice" });
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    dispose();
    resolveCommit(Response.json({ ...backendSettingsSnapshotFixture, settings_revision: 4, selected_live2d_model_id: "Rice" }));
    await expect(submission).rejects.toThrow("人物设置连接已变化");
    expect(events).toEqual([]);
    expect(statuses).not.toContain("failed");
  });

  it("较新设置仍选同一目标时保存成功，保持较新快捷键而不回退快照", async () => {
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    let resolveCommit: (response: Response) => void = () => undefined;
    globalThis.fetch = vi.fn().mockResolvedValueOnce(Response.json(backendSettingsSnapshotFixture))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { resolveCommit = resolve; }));
    const events: BackendEvent[] = [];
    const controlSocket = new ControlSocket();
    const dispose = controlSocket.connect({ onEvent: event => events.push(event), onStatusChange() {} });
    const socket = FakeWebSocket.instances[0]!;
    socket.open();
    const submission = controlSocket.updateSettings({ selected_live2d_model_id: "Rice" });
    await vi.waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    const newerSnapshot = { ...backendSettingsSnapshotFixture, settings_revision: 5,
      selected_live2d_model_id: "Rice", selection_capture_shortcut: "CommandOrControl+Alt+X" };
    socket.message({ type: "settings.changed", payload: newerSnapshot });
    resolveCommit(Response.json({ ...backendSettingsSnapshotFixture, settings_revision: 4, selected_live2d_model_id: "Rice" }));
    await expect(submission).resolves.toBeUndefined();
    expect(events).toEqual([{ type: "settings.changed", payload: newerSnapshot }]);
    dispose();
  });

  it("真实连接下设置写回遇到网络失败时，不切换到 fallback，并显式抛出原始错误", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("settings endpoint down"));
    vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const { controlSocket, statuses } = createConnectedControlSocket();

    await expect(controlSocket.updateSettings(backendSettingsFixture)).rejects.toThrow("settings endpoint down");
    expect(statuses).toEqual(["failed"]);
  });

  it("首轮 WebSocket 连接失败时只报告断开状态，不发本地演示设置、回复或气泡事件", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({ connectTimeoutMs: 50, random: () => 0.5 });
    const statuses: ConnectionStatus[] = [];
    const events: BackendEvent[] = [];

    const dispose = controlSocket.connect({
      onEvent(event) {
        events.push(event);
      },
      onStatusChange(status) {
        statuses.push(status);
      }
    });

    const firstSocket = FakeWebSocket.instances[0];
    firstSocket?.error();

    expect(statuses).toEqual(["connecting", "disconnected"]);
    expect(events).toEqual([]);

    dispose();
  });

  it("真实控制连接在已连通后断开时，会自动重连而不是直接进入 fallback", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({ connectTimeoutMs: 50, random: () => 0.5 });
    const statuses: ConnectionStatus[] = [];
    const events: BackendEvent[] = [];

    const dispose = controlSocket.connect({
      onEvent(event) {
        events.push(event);
      },
      onStatusChange(status) {
        statuses.push(status);
      }
    });

    const firstSocket = FakeWebSocket.instances[0];
    expect(firstSocket?.url).toBe("ws://127.0.0.1:8765/ws/control");
    expect(statuses).toEqual(["connecting"]);

    firstSocket?.open();
    expect(statuses).toEqual(["connecting", "connected"]);

    firstSocket?.closeFromServer();
    expect(statuses).toEqual(["connecting", "connected", "disconnected"]);
    expect(events).toEqual([]);

    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(statuses).toEqual(["connecting", "connected", "disconnected", "connecting"]);

    const secondSocket = FakeWebSocket.instances[1];
    secondSocket?.open();
    expect(statuses).toEqual(["connecting", "connected", "disconnected", "connecting", "connected"]);

    dispose();
  });

  it("同连接拒绝 Activity revision 回退，新连接接受后端归零", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({
      connectTimeoutMs: 50,
      reconnectDelayMs: 100,
      random: () => 0.5
    });
    const revisions: number[] = [];
    const dispose = controlSocket.connect({
      onEvent(event) {
        if (event.type === "activities.snapshot_changed") {
          revisions.push(event.payload.revision);
        }
      },
      onStatusChange() {}
    });
    const firstSocket = FakeWebSocket.instances[0];
    firstSocket?.open();
    firstSocket?.message(activitySnapshotEvent(11));
    firstSocket?.message(activitySnapshotEvent(10));

    expect(revisions).toEqual([11]);

    firstSocket?.closeFromServer();
    vi.advanceTimersByTime(100);
    const secondSocket = FakeWebSocket.instances[1];
    secondSocket?.open();
    secondSocket?.message(activitySnapshotEvent(0));

    expect(revisions).toEqual([11, 0]);
    dispose();
  });

  it("坏传输帧和坏公共事件只产生脱敏诊断，不泄露原始正文", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
    const reportDiagnostic = vi.fn();
    const controlSocket = new ControlSocket({
      connectTimeoutMs: 50,
      diagnosticsBridge: { reportDiagnostic }
    });
    const receivedEvents: BackendEvent[] = [];
    const dispose = controlSocket.connect({
      onEvent(event) {
        receivedEvents.push(event);
      },
      onStatusChange() {}
    });
    const socket = FakeWebSocket.instances[0];
    socket?.open();

    socket?.rawMessage(new Uint8Array([1, 2, 3]));
    socket?.rawMessage("{SECRET_PROVIDER_TOKEN");
    socket?.message({
      type: "user.partial_transcript",
      payload: { text: "SECRET_PROVIDER_TOKEN" }
    });

    expect(receivedEvents).toEqual([]);
    expect(reportDiagnostic.mock.calls).toEqual([
      ["warn", "control.event_rejected", {
        reason_code: "invalid_transport_frame",
        event_type: null
      }],
      ["warn", "control.event_rejected", {
        reason_code: "invalid_json",
        event_type: null
      }],
      ["warn", "control.event_rejected", {
        reason_code: "invalid_payload",
        event_type: "user.partial_transcript"
      }]
    ]);
    expect(JSON.stringify(reportDiagnostic.mock.calls)).not.toContain("SECRET_PROVIDER_TOKEN");

    dispose();
  });

  it("真实控制连接断开后的重连尝试超时时，不会降级成 fallback 或发出默认设置", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({
      connectTimeoutMs: 50,
      reconnectDelayMs: 100,
      random: () => 0.5
    });
    const statuses: ConnectionStatus[] = [];
    const events: BackendEvent[] = [];

    const dispose = controlSocket.connect({
      onEvent(event) {
        events.push(event);
      },
      onStatusChange(status) {
        statuses.push(status);
      }
    });

    const firstSocket = FakeWebSocket.instances[0];
    firstSocket?.open();
    firstSocket?.closeFromServer();

    vi.advanceTimersByTime(100);

    const reconnectingSocket = FakeWebSocket.instances[1];
    expect(reconnectingSocket).toBeTruthy();

    vi.advanceTimersByTime(50);

    expect(statuses).toEqual(["connecting", "connected", "disconnected", "connecting", "disconnected"]);
    expect(statuses).not.toContain("fallback");
    expect(events.some((event) => event.type === "settings.changed")).toBe(false);

    dispose();
  });

  it("真实控制连接断开后的重连尝试发生错误时，不会降级成 fallback 或发出默认设置", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({
      connectTimeoutMs: 50,
      reconnectDelayMs: 100,
      random: () => 0.5
    });
    const statuses: ConnectionStatus[] = [];
    const events: BackendEvent[] = [];

    const dispose = controlSocket.connect({
      onEvent(event) {
        events.push(event);
      },
      onStatusChange(status) {
        statuses.push(status);
      }
    });

    const firstSocket = FakeWebSocket.instances[0];
    firstSocket?.open();
    firstSocket?.closeFromServer();

    vi.advanceTimersByTime(100);

    const reconnectingSocket = FakeWebSocket.instances[1];
    expect(reconnectingSocket).toBeTruthy();

    reconnectingSocket?.error();

    expect(statuses).toEqual(["connecting", "connected", "disconnected", "connecting", "disconnected"]);
    expect(statuses).not.toContain("fallback");
    expect(events.some((event) => event.type === "settings.changed")).toBe(false);

    dispose();
  });

  it("连接稳定十秒后，下一次断线从首轮退避重新开始", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({
      connectTimeoutMs: 50,
      random: () => 0.5
    });
    const dispose = controlSocket.connect({
      onEvent() {},
      onStatusChange() {}
    });
    const firstSocket = FakeWebSocket.instances[0];
    firstSocket?.open();
    vi.advanceTimersByTime(10_000);
    firstSocket?.closeFromServer();

    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    dispose();
  });

  it("连接提前断开时只保留新的重连计时器", () => {
    vi.useFakeTimers();
    installWindowTimerShim();
    globalThis.WebSocket = FakeWebSocket as unknown as typeof WebSocket;

    const controlSocket = new ControlSocket({ connectTimeoutMs: 50, random: () => 0.5 });
    const dispose = controlSocket.connect({
      onEvent() {},
      onStatusChange() {}
    });
    const firstSocket = FakeWebSocket.instances[0];
    firstSocket?.open();
    firstSocket?.closeFromServer();

    expect(vi.getTimerCount()).toBe(1);
    dispose();
  });
});

function activitySnapshotEvent(revision: number): object {
  return {
    type: "activities.snapshot_changed",
    payload: {
      revision,
      runtime_id: "00000000-0000-4000-8000-0000000000aa",
      active: [],
      recent: []
    }
  };
}
