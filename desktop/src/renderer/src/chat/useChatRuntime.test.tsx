// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import sharedDefaultSettings from "../../../../../shared/default-settings.json";
import type { BackendEvent } from "../types/backendEvents";
import type { ConnectionStatus } from "../types/events";
import type { MaterialContextSnapshot } from "../materials/materialTypes";
import { useChatRuntime } from "./useChatRuntime";
import { ChatStatusRail } from "./ChatStatusRail";
import type { LlmAccountStatusSnapshot } from "./llmAccountStatus";

const mocks = vi.hoisted(() => ({
  fetchCurrentMaterialContext: vi.fn(),
  fetchActiveCharacterName: vi.fn(),
  fetchLlmAccountStatus: vi.fn(),
  cancelActivity: vi.fn(),
  cancelTimedReminder: vi.fn(),
  dismissTimedReminders: vi.fn(),
  updateTimedReminder: vi.fn(),
  deleteMaterialItem: vi.fn(),
  latestHandlers: null as null | {
    onEvent(event: BackendEvent): void;
    onStatusChange(status: ConnectionStatus): void;
  }
}));

vi.mock("../lib/materialsClient", () => ({
  fetchCurrentMaterialContext: mocks.fetchCurrentMaterialContext
}));

vi.mock("../lib/activityClient", () => ({
  cancelActivity: mocks.cancelActivity
}));

vi.mock("../lib/timedReminderClient", () => ({
  cancelTimedReminder: mocks.cancelTimedReminder,
  dismissTimedReminders: mocks.dismissTimedReminders,
  updateTimedReminder: mocks.updateTimedReminder
}));

vi.mock("./characterName", () => ({
  ACTIVE_CHARACTER_NAME_FALLBACK: "当前角色",
  fetchActiveCharacterName: mocks.fetchActiveCharacterName
}));

vi.mock("./llmAccountStatus", () => ({
  fetchLlmAccountStatus: mocks.fetchLlmAccountStatus
}));

vi.mock("../lib/controlSocket", () => ({
  ControlSocket: class {
    fetchSettings = vi.fn(async () => ({
      show_speech_bubbles: sharedDefaultSettings.show_speech_bubbles,
      followup_toggle_shortcut_enabled: sharedDefaultSettings.followup_toggle_shortcut_enabled,
      followup_toggle_shortcut: sharedDefaultSettings.followup_toggle_shortcut,
      avatar_visibility_shortcut_enabled: sharedDefaultSettings.avatar_visibility_shortcut_enabled,
      avatar_visibility_shortcut: sharedDefaultSettings.avatar_visibility_shortcut,
      selection_capture_shortcut_enabled: sharedDefaultSettings.selection_capture_shortcut_enabled,
      selection_capture_shortcut: sharedDefaultSettings.selection_capture_shortcut,
      selected_live2d_model_id: sharedDefaultSettings.selected_live2d_model_id
    }));

    connect = vi.fn((handlers: {
      onEvent(event: BackendEvent): void;
      onStatusChange(status: ConnectionStatus): void;
    }) => {
      mocks.latestHandlers = handlers;
      handlers.onStatusChange("connecting");
      handlers.onStatusChange("connected");
      return vi.fn();
    });
  }
}));

describe("useChatRuntime material contexts", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let latestRuntime: ReturnType<typeof useChatRuntime> | null = null;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    mocks.fetchCurrentMaterialContext.mockReset();
    mocks.fetchActiveCharacterName.mockReset().mockResolvedValue(null);
    mocks.fetchLlmAccountStatus.mockReset().mockRejectedValue(new Error("account status unavailable"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    mocks.cancelActivity.mockReset().mockResolvedValue(undefined);
    mocks.cancelTimedReminder.mockReset().mockResolvedValue(undefined);
    mocks.dismissTimedReminders.mockReset().mockResolvedValue({
      runtimeId: "runtime-reminder",
      dismissedIds: ["reminder-ringing"]
    });
    mocks.updateTimedReminder.mockReset().mockResolvedValue(undefined);
    mocks.deleteMaterialItem.mockReset();
    mocks.latestHandlers = null;
    latestRuntime = null;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    window.companionChat = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        }
      },
      removeStagedMaterial: vi.fn(async (materialId: string) => ({
        accepted: true,
        materialContext: await mocks.deleteMaterialItem(materialId),
        removedDisplayName: "a.md"
      })),
      forgetRecentMaterial: vi.fn(),
      confirmMaterialRemoved: vi.fn(async () => ({ accepted: true }))
    };
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    delete window.companionChat;
    vi.unstubAllGlobals();
  });

  it("打开聊天窗口时读取当前材料上下文，并用 contexts_changed 事件实时更新", async () => {
    const initialContext: MaterialContextSnapshot = {
      revision: 1,
      turn_material_context: {
        staged_materials: [makeMaterialItem({ material_id: "material-1", display_name: "a.md" })],
        active: null
      },
      recent_materials: []
    };
    const nextContext: MaterialContextSnapshot = {
      revision: 2,
      turn_material_context: {
        staged_materials: [makeMaterialItem({ material_id: "material-2", display_name: "b.md" })],
        active: null
      },
      recent_materials: []
    };
    mocks.fetchCurrentMaterialContext.mockResolvedValue(initialContext);

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    expect(mocks.fetchCurrentMaterialContext).toHaveBeenCalledWith("http://127.0.0.1:8765");
    expect(latestRuntime?.materialContext).toEqual(initialContext);

    act(() => {
      mocks.latestHandlers?.onEvent({
        type: "materials.contexts_changed",
        payload: nextContext
      });
    });

    expect(latestRuntime?.materialContext).toEqual(nextContext);
  });

  it("删除材料项后通过材料上下文客户端请求后端删除并刷新当前上下文", async () => {
    const initialContext: MaterialContextSnapshot = {
      revision: 1,
      turn_material_context: {
        staged_materials: [makeMaterialItem({ material_id: "material-1", display_name: "a.md" })],
        active: null
      },
      recent_materials: []
    };
    const afterDeleteContext: MaterialContextSnapshot = {
      revision: 2,
      turn_material_context: {
        staged_materials: [],
        active: null
      },
      recent_materials: []
    };
    mocks.fetchCurrentMaterialContext.mockResolvedValue(initialContext);
    mocks.deleteMaterialItem.mockResolvedValue(afterDeleteContext);

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    await act(async () => {
      await latestRuntime?.removeMaterialItem("material-1");
    });

    expect(mocks.deleteMaterialItem).toHaveBeenCalledWith("material-1");
    expect(window.companionChat?.confirmMaterialRemoved).toHaveBeenCalledWith("material-1", "a.md");
    expect(mocks.fetchCurrentMaterialContext).toHaveBeenCalledTimes(1);
    expect(latestRuntime?.materialContext).toEqual(afterDeleteContext);
  });

  it("删除失败时保留材料并展示可重试错误", async () => {
    const initialContext: MaterialContextSnapshot = {
      revision: 1,
      turn_material_context: {
        staged_materials: [makeMaterialItem({ material_id: "material-1" })],
        active: null
      },
      recent_materials: []
    };
    mocks.fetchCurrentMaterialContext.mockResolvedValue(initialContext);
    mocks.deleteMaterialItem.mockRejectedValue(new Error("network"));

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    await act(async () => {
      await expect(latestRuntime?.removeMaterialItem("material-1")).rejects.toThrow("network");
    });

    expect(latestRuntime?.materialContext).toEqual(initialContext);
    expect(latestRuntime?.materialMutationError).toBe("材料删除失败，请重试");
    expect(window.companionChat?.reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "materials.staged_delete_failed",
      expect.any(Error)
    );
  });

  it("资料卡回答复用正常 submitText 轮次并保留当前暂存材料", async () => {
    const submitTurn = vi.fn(async (input: { submissionId: string; messageText: string }) => ({
      accepted: true,
      turnSubmissions: {
        revision: 1,
        items: [{
          submissionId: input.submissionId,
          messageText: input.messageText,
          state: "preparing",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    }));
    mocks.fetchCurrentMaterialContext.mockResolvedValue({
      revision: 1,
      turn_material_context: {
        staged_materials: [makeMaterialItem({ material_id: "material-1" })],
        active: null
      },
      recent_materials: []
    });
    window.companionChat = {
      ...window.companionChat!,
      submitTurn
    };

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    await act(async () => {
      await latestRuntime?.submitWorkInteractionAction({
        requestId: "question-a",
        kind: "question",
        messageText: "我对当前工作的补充回答：\n- 输出格式？：MP3"
      });
    });

    expect(submitTurn).toHaveBeenCalledWith(expect.objectContaining({
      submissionId: expect.stringMatching(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
      ),
      messageText: "我对当前工作的补充回答：\n- 输出格式？：MP3"
    }));
    expect(latestRuntime?.state.messages[0]).toMatchObject({
      text: "我对当前工作的补充回答：\n- 输出格式？：MP3",
      submissionState: "preparing",
      submissionAcknowledged: true
    });
    expect(latestRuntime?.materialContext?.turn_material_context.staged_materials).toHaveLength(1);
  });

  it("reservation 失败时撤销乐观气泡并暴露 Composer 错误", async () => {
    const submitTurn = vi.fn(async () => {
      throw new Error("reservation unavailable");
    });
    mocks.fetchCurrentMaterialContext.mockResolvedValue({
      revision: 0,
      turn_material_context: { staged_materials: [], active: null },
      recent_materials: []
    });
    window.companionChat = { ...window.companionChat!, submitTurn };

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    await act(async () => {
      await expect(latestRuntime?.submitText("保留在输入框的正文")).rejects.toThrow(
        "reservation unavailable"
      );
    });

    expect(latestRuntime?.state.messages).toEqual([]);
    expect(latestRuntime?.textSubmitError).toBe("文本发送失败，请稍后重试。");
  });

  it("连续文本可并发 reservation，并抵御先返回的新快照被旧响应覆盖", async () => {
    const firstResponse = createDeferred<unknown>();
    const submittedItems: Array<{
      submissionId: string;
      messageText: string;
      state: "preparing";
      createdOrder: number;
      errorMessage: null;
    }> = [];
    const submitTurn = vi.fn((input: { submissionId: string; messageText: string }) => {
      submittedItems.push({
        ...input,
        state: "preparing",
        createdOrder: submittedItems.length + 1,
        errorMessage: null
      });
      if (submittedItems.length === 1) {
        return firstResponse.promise;
      }
      return Promise.resolve({
        accepted: true,
        turnSubmissions: { revision: 2, items: [...submittedItems] }
      });
    });
    mocks.fetchCurrentMaterialContext.mockResolvedValue({
      revision: 0,
      turn_material_context: { staged_materials: [], active: null },
      recent_materials: []
    });
    window.companionChat = { ...window.companionChat!, submitTurn };

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    let firstSubmission!: Promise<void>;
    await act(async () => {
      firstSubmission = latestRuntime!.submitText("第一条");
      await Promise.resolve();
      await latestRuntime!.submitText("第二条");
    });
    await act(async () => {
      firstResponse.resolve({
        accepted: true,
        turnSubmissions: { revision: 1, items: [submittedItems[0]] }
      });
      await firstSubmission;
    });

    expect(submitTurn).toHaveBeenCalledTimes(2);
    expect(latestRuntime?.state.turnSubmissionsRevision).toBe(2);
    expect(latestRuntime?.state.messages.map(message => message.text)).toEqual([
      "第一条",
      "第二条"
    ]);
    expect(latestRuntime?.state.messages.every(
      message => message.submissionAcknowledged === true
    )).toBe(true);
  });

  it("主动刷新复用当前快照客户端和 revision reducer", async () => {
    const initialContext = emptyMaterialContext(1);
    const refreshedContext: MaterialContextSnapshot = {
      ...emptyMaterialContext(2),
      recent_materials: [makeRecentMaterial()]
    };
    mocks.fetchCurrentMaterialContext.mockResolvedValueOnce(initialContext);

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    mocks.fetchCurrentMaterialContext.mockResolvedValueOnce(refreshedContext);

    await act(async () => {
      await latestRuntime?.refreshMaterialContext();
    });

    expect(mocks.fetchCurrentMaterialContext).toHaveBeenCalledTimes(2);
    expect(latestRuntime?.materialContext).toEqual(refreshedContext);
  });

  it("控制连接重连时清空旧材料 revision 并接纳新 Backend 低版本快照", async () => {
    const oldBackendContext: MaterialContextSnapshot = {
      ...emptyMaterialContext(9),
      recent_materials: [makeRecentMaterial()]
    };
    const restartedBackendContext = emptyMaterialContext(0);
    mocks.fetchCurrentMaterialContext.mockResolvedValue(oldBackendContext);

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    expect(latestRuntime?.materialContext).toEqual(oldBackendContext);

    act(() => {
      mocks.latestHandlers?.onStatusChange("connecting");
    });
    expect(latestRuntime?.materialContext).toBeNull();

    act(() => {
      mocks.latestHandlers?.onEvent({
        type: "materials.contexts_changed",
        payload: restartedBackendContext
      });
    });

    expect(latestRuntime?.materialContext).toEqual(restartedBackendContext);
  });

  it("旧 Backend 的迟到 HTTP 快照不能回填新连接世代", async () => {
    const oldResponse = createDeferred<MaterialContextSnapshot>();
    const oldBackendContext: MaterialContextSnapshot = {
      ...emptyMaterialContext(9),
      recent_materials: [makeRecentMaterial()]
    };
    const restartedBackendContext = emptyMaterialContext(0);
    mocks.fetchCurrentMaterialContext.mockReturnValue(oldResponse.promise);

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => {
      mocks.latestHandlers?.onStatusChange("connecting");
      mocks.latestHandlers?.onEvent({
        type: "materials.contexts_changed",
        payload: restartedBackendContext
      });
    });

    await act(async () => {
      oldResponse.resolve(oldBackendContext);
      await oldResponse.promise;
    });

    expect(latestRuntime?.materialContext).toEqual(restartedBackendContext);
  });

  it("忘记成功应用完整快照，失败时保留原项并显示统一错误", async () => {
    const initialContext: MaterialContextSnapshot = {
      ...emptyMaterialContext(1),
      recent_materials: [makeRecentMaterial()]
    };
    const forgottenContext = emptyMaterialContext(2);
    mocks.fetchCurrentMaterialContext.mockResolvedValue(initialContext);
    const forgetRecentMaterial = vi.fn()
      .mockResolvedValueOnce({ accepted: true, materialContext: forgottenContext })
      .mockRejectedValueOnce(new Error("network"));
    window.companionChat = { ...window.companionChat!, forgetRecentMaterial };

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    await act(async () => {
      await latestRuntime?.forgetRecentMaterial("G:\\docs\\report.md");
    });
    expect(latestRuntime?.materialContext).toEqual(forgottenContext);

    act(() => {
      mocks.latestHandlers?.onEvent({
        type: "materials.contexts_changed",
        payload: { ...initialContext, revision: 3 }
      });
    });
    await act(async () => {
      await expect(latestRuntime?.forgetRecentMaterial("G:\\docs\\report.md")).rejects.toThrow("network");
    });

    expect(latestRuntime?.materialContext?.recent_materials).toEqual([makeRecentMaterial()]);
    expect(latestRuntime?.materialMutationError).toBe("忘记材料失败，请重试");
  });

  it("活动快照拒绝同一运行期回退，并在 Backend 重启后替换低 revision", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    const first = makeActivitySnapshot(5, "runtime-a", "activity-a");
    const stale = makeActivitySnapshot(4, "runtime-a", "activity-stale");
    const restarted = makeActivitySnapshot(0, "runtime-b", "activity-b");
    act(() => mocks.latestHandlers?.onEvent({ type: "activities.snapshot_changed", payload: first }));
    act(() => mocks.latestHandlers?.onEvent({ type: "activities.snapshot_changed", payload: stale }));
    expect(latestRuntime?.workspace.activitySnapshot).toBe(first);

    act(() => mocks.latestHandlers?.onEvent({ type: "activities.snapshot_changed", payload: restarted }));
    expect(latestRuntime?.workspace.activitySnapshot).toBe(restarted);
  });

  it("提醒快照在运行期切换时允许低 revision 接管并清空旧请求", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    const first = makeReminderSnapshot(8, "runtime-reminder-a", "reminder-a", "scheduled");
    const stale = makeReminderSnapshot(7, "runtime-reminder-a", "reminder-stale", "scheduled");
    const restarted = makeReminderSnapshot(0, "runtime-reminder-b", "reminder-b", "scheduled");
    act(() => mocks.latestHandlers?.onEvent({ type: "reminders.snapshot_changed", payload: first }));
    act(() => mocks.latestHandlers?.onEvent({ type: "reminders.snapshot_changed", payload: stale }));
    expect(latestRuntime?.workspace.reminderSnapshot).toBe(first);

    act(() => mocks.latestHandlers?.onEvent({ type: "reminders.snapshot_changed", payload: restarted }));
    expect(latestRuntime?.workspace.reminderSnapshot).toBe(restarted);
    expect(latestRuntime?.workspace.runtimeRestartMessage).toContain("Backend 已重启");
  });

  it("取消活动任务期间防重复，并等待权威快照移除请求占位", async () => {
    const deferred = createDeferred<void>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.cancelActivity.mockReturnValueOnce(deferred.promise);
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "activities.snapshot_changed",
      payload: makeActivitySnapshot(1, "runtime-a", "activity-a")
    }));

    let request!: Promise<void>;
    act(() => { request = latestRuntime!.workspace.requestCancelActivity("activity-a"); });
    expect(latestRuntime?.workspace.requestingActivityIds).toEqual(new Set(["activity-a"]));
    await act(async () => { await latestRuntime?.workspace.requestCancelActivity("activity-a"); });
    expect(mocks.cancelActivity).toHaveBeenCalledTimes(1);

    deferred.resolve();
    await act(async () => { await request; });
    expect(latestRuntime?.workspace.requestingActivityIds).toEqual(new Set(["activity-a"]));
    act(() => mocks.latestHandlers?.onEvent({
      type: "activities.snapshot_changed",
      payload: { ...makeActivitySnapshot(2, "runtime-a", "activity-a"), active: [] }
    }));
    expect(latestRuntime?.workspace.requestingActivityIds).toEqual(new Set());
  });

  it("取消活动任务 HTTP 失败时保留权威任务并清除请求占位", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.cancelActivity.mockRejectedValueOnce(new Error("任务服务不可用"));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "activities.snapshot_changed",
      payload: makeActivitySnapshot(1, "runtime-a", "activity-a")
    }));

    await act(async () => { await latestRuntime?.workspace.requestCancelActivity("activity-a"); });
    expect(latestRuntime?.workspace.activitySnapshot.active).toHaveLength(1);
    expect(latestRuntime?.workspace.requestingActivityIds).toEqual(new Set());
    expect(latestRuntime?.workspace.errors.activity).toBe("任务服务不可用");
  });

  it("旧运行期的活动取消回包不能清理新运行期同编号任务的状态", async () => {
    const deferred = createDeferred<void>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.cancelActivity.mockReturnValueOnce(deferred.promise);
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "activities.snapshot_changed",
      payload: makeActivitySnapshot(1, "runtime-a", "activity-same")
    }));
    let oldRequest!: Promise<void>;
    act(() => { oldRequest = latestRuntime!.workspace.requestCancelActivity("activity-same"); });

    act(() => mocks.latestHandlers?.onEvent({
      type: "activities.snapshot_changed",
      payload: makeActivitySnapshot(0, "runtime-b", "activity-same")
    }));
    expect(latestRuntime?.workspace.requestingActivityIds).toEqual(new Set());

    deferred.resolve();
    await act(async () => { await oldRequest; });
    expect(latestRuntime?.workspace.requestingActivityIds).toEqual(new Set());
    expect(latestRuntime?.workspace.errors.activity).toBeNull();
  });

  it("知道了只接受当前运行期、不重复且响铃中的提醒编号", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "reminders.snapshot_changed",
      payload: makeReminderSnapshot(1, "runtime-reminder-a", "reminder-ringing", "ringing")
    }));

    await act(async () => {
      await latestRuntime?.workspace.requestDismissReminders("wrong-runtime", ["reminder-ringing"]);
      await latestRuntime?.workspace.requestDismissReminders("runtime-reminder-a", ["reminder-ringing", "reminder-ringing"]);
      await latestRuntime?.workspace.requestDismissReminders("runtime-reminder-a", ["missing"]);
      await latestRuntime?.workspace.requestDismissReminders("runtime-reminder-a", ["reminder-ringing"]);
    });
    expect(mocks.dismissTimedReminders).toHaveBeenCalledTimes(1);
    expect(mocks.dismissTimedReminders).toHaveBeenCalledWith(
      "http://127.0.0.1:8765",
      "runtime-reminder-a",
      ["reminder-ringing"]
    );
  });

  it("提醒修改、取消和关闭失败时保留权威状态并暴露错误", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.updateTimedReminder.mockRejectedValueOnce(new Error("修改失败"));
    mocks.cancelTimedReminder.mockRejectedValueOnce(new Error("取消失败"));
    mocks.dismissTimedReminders.mockRejectedValueOnce(new Error("关闭失败"));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "reminders.snapshot_changed",
      payload: makeReminderSnapshot(1, "runtime-reminder-a", "reminder-scheduled", "scheduled")
    }));

    await act(async () => {
      await expect(latestRuntime?.workspace.requestUpdateReminder("reminder-scheduled", { content: "新的提醒" })).rejects.toThrow("修改失败");
    });
    expect(latestRuntime?.workspace.errors.reminder).toBe("修改失败");

    await act(async () => { await latestRuntime?.workspace.requestCancelReminder("reminder-scheduled"); });
    expect(latestRuntime?.workspace.errors.reminder).toBe("取消失败");

    act(() => mocks.latestHandlers?.onEvent({
      type: "reminders.snapshot_changed",
      payload: makeReminderSnapshot(2, "runtime-reminder-a", "reminder-ringing", "ringing")
    }));
    await act(async () => { await latestRuntime?.workspace.requestDismissReminders("runtime-reminder-a", ["reminder-ringing"]); });
    expect(latestRuntime?.workspace.errors.reminder).toBe("关闭失败");
  });

  it("settings 快照只把安全的已启用 Skill 投影到右栏，旧回包不能覆盖新请求", async () => {
    const firstFetch = createDeferred<{ ok: boolean; json(): Promise<unknown> }>();
    const secondFetch = createDeferred<{ ok: boolean; json(): Promise<unknown> }>();
    vi.stubGlobal("fetch", vi.fn()
      .mockReturnValueOnce(firstFetch.promise)
      .mockReturnValueOnce(secondFetch.promise));
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    const settingsEvent = (skills: Record<string, boolean>) => ({
      type: "settings.changed",
      payload: { background_agent: { skills } }
    }) as unknown as BackendEvent;
    act(() => mocks.latestHandlers?.onEvent(settingsEvent({ first: true })));
    act(() => mocks.latestHandlers?.onEvent(settingsEvent({ second: true })));
    firstFetch.resolve({ ok: true, json: async () => ({ skills: [
      { id: "first", name: "旧结果", enabled: true, ready: true, usable: true }
    ] }) });
    secondFetch.resolve({ ok: true, json: async () => ({ skills: [
      { id: "second", name: "新结果", enabled: true, ready: true, usable: true },
      { id: "unsafe/path", name: "内部路径", enabled: true, ready: true, usable: true }
    ] }) });
    await act(async () => {
      await Promise.all([firstFetch.promise, secondFetch.promise]);
      await Promise.resolve();
    });
    expect(latestRuntime?.workspace.enabledSkills).toEqual([
      { id: "second", name: "新结果", ready: true, usable: true }
    ]);
  });

  it("Bootstrap 状态变化后刷新模型账户余额并投影到工作区", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchLlmAccountStatus.mockResolvedValue({
      providers: [{
        provider_id: "deepseek",
        display_name: "DeepSeek",
        active: true,
        status: "available",
        balance: "12.50",
        currency: "CNY",
        fetched_at: "2026-09-25T05:00:00Z",
        message: null
      }]
    });

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "bootstrap.status_changed",
      payload: {}
    } as unknown as BackendEvent));
    await act(async () => {
      await Promise.resolve();
    });

    expect(mocks.fetchLlmAccountStatus).toHaveBeenCalledWith("http://127.0.0.1:8765");
    expect(latestRuntime?.workspace.llmAccountStatus?.providers[0]).toMatchObject({
      provider_id: "deepseek",
      balance: "12.50"
    });
  });

  it("余额请求失败不冒充空账户，重试成功后清除错误", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchLlmAccountStatus.mockRejectedValueOnce(new Error("HTTP 404"));
    await act(async () => { root.render(<RuntimeProbe />); });
    expect(latestRuntime?.workspace.llmAccountStatus).toBeUndefined();

    await act(async () => {
      mocks.latestHandlers?.onEvent({ type: "bootstrap.status_changed", payload: {} } as unknown as BackendEvent);
    });
    expect(latestRuntime?.workspace.llmAccountStatus).toBeUndefined();
    expect(latestRuntime?.workspace.llmAccountStatusError).toBe("余额读取失败，请重试");

    mocks.fetchLlmAccountStatus.mockResolvedValueOnce({ providers: [] });
    await act(async () => { await latestRuntime?.workspace.refreshLlmAccountStatus?.(); });
    expect(latestRuntime?.workspace.llmAccountStatus).toEqual({ providers: [] });
    expect(latestRuntime?.workspace.llmAccountStatusError).toBeNull();
  });

  it("HTTP 成功但厂商返回错误时可重试，读取期间隐藏按钮，成功后显示余额", async () => {
    const retryResponse = createDeferred<LlmAccountStatusSnapshot>();
    const failedProvider = {
      provider_id: "deepseek",
      display_name: "DeepSeek",
      active: true,
      status: "error" as const,
      balance: null,
      currency: null,
      fetched_at: "2026-09-25T05:00:00Z",
      message: "余额服务暂时不可用"
    };
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchLlmAccountStatus.mockResolvedValueOnce({ providers: [failedProvider] });
    await act(async () => { root.render(<RuntimeProbe showStatusRail />); });
    await act(async () => {
      mocks.latestHandlers?.onEvent({ type: "bootstrap.status_changed", payload: {} } as unknown as BackendEvent);
    });
    expect(latestRuntime?.workspace.llmAccountStatusError).toBeNull();
    expect(container.textContent).toContain("余额服务暂时不可用");
    const retryButton = Array.from(container.querySelectorAll("button"))
      .find(button => button.textContent === "重新读取余额");
    expect(retryButton).toBeDefined();

    mocks.fetchLlmAccountStatus.mockReturnValueOnce(retryResponse.promise);
    act(() => { retryButton!.click(); });
    expect(mocks.fetchLlmAccountStatus).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("正在读取余额…");
    expect(container.textContent).not.toContain("重新读取余额");

    await act(async () => {
      retryResponse.resolve({ providers: [{
        ...failedProvider, status: "available", balance: "12.50", currency: "CNY", message: null
      }] });
    });
    expect(container.textContent).toContain("余额 12.50 CNY");
    expect(container.textContent).not.toContain("重新读取余额");
    expect(container.textContent).not.toContain("余额服务暂时不可用");
  });

  it("刷新余额期间清掉旧账户结果，迟到响应不能覆盖新结果", async () => {
    const oldResponse = createDeferred<{ providers: [] }>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchLlmAccountStatus.mockResolvedValueOnce({ providers: [] });
    await act(async () => { root.render(<RuntimeProbe />); });
    const bootstrapEvent = { type: "bootstrap.status_changed", payload: {} } as unknown as BackendEvent;
    await act(async () => { mocks.latestHandlers?.onEvent(bootstrapEvent); });
    expect(latestRuntime?.workspace.llmAccountStatus).toEqual({ providers: [] });

    mocks.fetchLlmAccountStatus.mockReturnValueOnce(oldResponse.promise).mockRejectedValueOnce(new Error("HTTP 503"));
    act(() => { mocks.latestHandlers?.onEvent(bootstrapEvent); });
    expect(latestRuntime?.workspace.llmAccountStatus).toBeUndefined();
    await act(async () => { mocks.latestHandlers?.onEvent(bootstrapEvent); });
    await act(async () => { oldResponse.resolve({ providers: [] }); });
    expect(latestRuntime?.workspace.llmAccountStatus).toBeUndefined();
    expect(latestRuntime?.workspace.llmAccountStatusError).toBe("余额读取失败，请重试");
  });

  it("重连后忽略旧余额响应，并由新连接首帧重新读取", async () => {
    const oldResponse = createDeferred<{ providers: [] }>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchLlmAccountStatus.mockReturnValueOnce(oldResponse.promise);
    await act(async () => { root.render(<RuntimeProbe />); });
    const bootstrapEvent = { type: "bootstrap.status_changed", payload: {} } as unknown as BackendEvent;
    act(() => { mocks.latestHandlers?.onEvent(bootstrapEvent); });

    act(() => { mocks.latestHandlers?.onStatusChange("connecting"); });
    await act(async () => { oldResponse.resolve({ providers: [] }); });
    expect(latestRuntime?.workspace.llmAccountStatus).toBeUndefined();
    expect(latestRuntime?.workspace.llmAccountStatusError).toBeNull();

    mocks.fetchLlmAccountStatus.mockResolvedValueOnce({ providers: [] });
    await act(async () => {
      mocks.latestHandlers?.onStatusChange("connected");
      mocks.latestHandlers?.onEvent(bootstrapEvent);
    });
    expect(mocks.fetchLlmAccountStatus).toHaveBeenCalledTimes(2);
    expect(latestRuntime?.workspace.llmAccountStatus).toEqual({ providers: [] });
  });

  it("使用当前激活角色卡名称，并在设置快照变化后刷新", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchActiveCharacterName
      .mockResolvedValueOnce("星澜")
      .mockResolvedValueOnce("焰灵");

    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    expect(latestRuntime?.state.companionName).toBe("星澜");

    const settingsEvent = {
      type: "settings.changed",
      payload: { background_agent: { skills: {} } }
    } as unknown as BackendEvent;
    act(() => mocks.latestHandlers?.onEvent(settingsEvent));
    await act(async () => {
      await Promise.resolve();
    });

    expect(latestRuntime?.state.companionName).toBe("焰灵");
    expect(mocks.fetchActiveCharacterName).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8765"
    );
    expect(mocks.fetchActiveCharacterName).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8765"
    );
  });

  it("角色名称请求失败时保留已显示的当前角色名", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchActiveCharacterName.mockResolvedValueOnce("星澜");
    await act(async () => {
      root.render(<RuntimeProbe />);
    });
    expect(latestRuntime?.state.companionName).toBe("星澜");

    await act(async () => {
      mocks.latestHandlers?.onStatusChange("connecting");
      mocks.latestHandlers?.onStatusChange("connected");
    });
    expect(latestRuntime?.state.companionName).toBe("星澜");
  });

  it("角色卡切换后较旧的名称请求不能覆盖最新结果", async () => {
    const oldName = createDeferred<string | null>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchActiveCharacterName
      .mockReturnValueOnce(oldName.promise)
      .mockResolvedValueOnce("新角色");
    await act(async () => {
      root.render(<RuntimeProbe />);
    });
    expect(latestRuntime?.state.companionName).toBe("当前角色");

    await act(async () => {
      mocks.latestHandlers?.onEvent({
        type: "settings.changed",
        payload: { background_agent: { skills: {} } }
      } as unknown as BackendEvent);
    });
    expect(latestRuntime?.state.companionName).toBe("新角色");

    await act(async () => { oldName.resolve("旧角色"); });
    expect(latestRuntime?.state.companionName).toBe("新角色");
  });

  it("重新连接期间迟到的旧角色名称不能回填新连接", async () => {
    const oldName = createDeferred<string | null>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchActiveCharacterName
      .mockReturnValueOnce(oldName.promise)
      .mockResolvedValueOnce("重连后的角色");
    await act(async () => { root.render(<RuntimeProbe />); });

    act(() => mocks.latestHandlers?.onStatusChange("connecting"));
    await act(async () => { oldName.resolve("旧角色"); });
    expect(latestRuntime?.state.companionName).toBe("当前角色");

    await act(async () => { mocks.latestHandlers?.onStatusChange("connected"); });
    expect(latestRuntime?.state.companionName).toBe("重连后的角色");
  });

  it("运行时卸载后旧名称请求不能覆盖重新挂载的角色", async () => {
    const oldName = createDeferred<string | null>();
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    mocks.fetchActiveCharacterName
      .mockReturnValueOnce(oldName.promise)
      .mockResolvedValueOnce("重新挂载后的角色");
    await act(async () => { root.render(<RuntimeProbe />); });
    act(() => root.render(null));
    await act(async () => { root.render(<RuntimeProbe />); });
    await act(async () => { oldName.resolve("已卸载的角色"); });

    expect(latestRuntime?.state.companionName).toBe("重新挂载后的角色");
  });

  it("实时转写只保留 partial 文本，final 事件清空并进入思考态", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime",
        input_runtime_epoch: 1,
        turn_id: "turn-1",
        turn_sequence: 1,
        session_id: "session-1",
        text: "你好，今天"
      }
    }));
    expect(latestRuntime?.liveTranscript).toBe("你好，今天");
    expect(latestRuntime?.voiceState).toBe("listening");

    act(() => mocks.latestHandlers?.onEvent({
      type: "user.final_transcript",
      payload: {
        input_runtime_id: "input-runtime",
        input_runtime_epoch: 1,
        turn_id: "turn-1",
        turn_sequence: 1,
        session_id: "session-1",
        history_disposition: "ordinary_turn",
        text: "你好，今天"
      }
    }));
    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("thinking");
  });

  it("输入轮次在没有 final transcript 的情况下结束时也清空旧实时转写", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime",
        input_runtime_epoch: 1,
        turn_id: "turn-stale",
        turn_sequence: 3,
        session_id: "session-1",
        text: "之前识别到的内容"
      }
    }));
    expect(latestRuntime?.liveTranscript).toBe("之前识别到的内容");

    act(() => mocks.latestHandlers?.onEvent({
      type: "turn.state_changed",
      payload: {
        input_runtime_id: "input-runtime",
        input_runtime_epoch: 1,
        turn_id: "turn-stale",
        turn_sequence: 3,
        session_id: "session-1",
        turn_state: "idle",
        reason: "input_reset"
      }
    }));
    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("ready");
  });

  it("Final 之后迟到的同一 Turn partial 不能重新显示旧转写", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => { root.render(<RuntimeProbe />); });

    const voiceTurn = {
      input_runtime_id: "input-runtime-a",
      input_runtime_epoch: 1,
      turn_id: "turn-closed",
      turn_sequence: 3,
      session_id: "session-1"
    };
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: { ...voiceTurn, text: "已收口的文字" }
    }));
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.final_transcript",
      payload: { ...voiceTurn, history_disposition: "ordinary_turn", text: "已收口的文字" }
    }));
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: { ...voiceTurn, text: "迟到的旧文字" }
    }));

    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("thinking");
  });

  it("旧 Turn 的 idle 不能清掉更新 Turn 的实时转写", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => { root.render(<RuntimeProbe />); });

    const runtime = {
      input_runtime_id: "input-runtime-a",
      input_runtime_epoch: 1,
      session_id: "session-1"
    };
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: { ...runtime, turn_id: "turn-new", turn_sequence: 4, text: "新一轮正在说" }
    }));
    act(() => mocks.latestHandlers?.onEvent({
      type: "turn.state_changed",
      payload: {
        ...runtime,
        turn_id: "turn-old",
        turn_sequence: 3,
        turn_state: "idle",
        reason: "late_old_turn"
      }
    }));

    expect(latestRuntime?.liveTranscript).toBe("新一轮正在说");
    expect(latestRuntime?.voiceState).toBe("listening");
  });

  it("新的 Runtime 接管后，旧 Runtime 的 partial 不能复活转写", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => { root.render(<RuntimeProbe />); });

    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-old",
        input_runtime_epoch: 1,
        turn_id: "turn-old",
        turn_sequence: 1,
        session_id: "session-1",
        text: "旧运行期文字"
      }
    }));
    act(() => mocks.latestHandlers?.onEvent({
      type: "input.runtime_changed",
      payload: { input_runtime_id: "input-runtime-new", input_runtime_epoch: 2 }
    }));
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "input-runtime-old",
        input_runtime_epoch: 1,
        turn_id: "turn-old",
        turn_sequence: 1,
        session_id: "session-1",
        text: "迟到的旧运行期文字"
      }
    }));

    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("ready");
  });

  it("同一 Turn 的 listening 重放不会抹掉正在输入的实时转写", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => { root.render(<RuntimeProbe />); });
    const runtime = {
      input_runtime_id: "input-runtime-a",
      input_runtime_epoch: 1,
      turn_id: "turn-listening",
      turn_sequence: 2,
      session_id: "session-1"
    };
    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: { ...runtime, text: "还在说" }
    }));
    act(() => mocks.latestHandlers?.onEvent({
      type: "turn.state_changed",
      payload: { ...runtime, turn_state: "listening", reason: "replayed" }
    }));

    expect(latestRuntime?.liveTranscript).toBe("还在说");
    expect(latestRuntime?.voiceState).toBe("listening");
  });

  it("开放 Turn 尚无 partial 时，助手输出事件也不能抢走聆听状态", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => { root.render(<RuntimeProbe />); });
    act(() => mocks.latestHandlers?.onEvent({
      type: "turn.state_changed",
      payload: {
        input_runtime_id: "input-runtime-a",
        input_runtime_epoch: 1,
        turn_id: "turn-open",
        turn_sequence: 1,
        session_id: "session-1",
        turn_state: "listening",
        reason: "voice_turn_started"
      }
    } as unknown as BackendEvent));
    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("listening");

    act(() => mocks.latestHandlers?.onEvent({
      type: "assistant.audio_finished",
      payload: {
        request_id: "request-old",
        session_id: "session-1",
        generation_id: "generation-old"
      }
    } as unknown as BackendEvent));
    act(() => mocks.latestHandlers?.onEvent({
      type: "assistant.request_failed",
      payload: {
        request_id: "request-old",
        code: "output_unavailable",
        message: "旧输出失败"
      }
    } as unknown as BackendEvent));

    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("listening");
  });

  it("新的输入 Runtime 到来时不会保留上一 Runtime 的实时转写", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => {
      root.render(<RuntimeProbe />);
      await Promise.resolve();
    });

    act(() => mocks.latestHandlers?.onEvent({
      type: "user.partial_transcript",
      payload: {
        input_runtime_id: "old-runtime",
        input_runtime_epoch: 1,
        turn_id: "turn-old",
        turn_sequence: 1,
        session_id: "session-1",
        text: "旧运行期文字"
      }
    }));
    expect(latestRuntime?.liveTranscript).toBe("旧运行期文字");

    act(() => mocks.latestHandlers?.onEvent({
      type: "input.runtime_changed",
      payload: {
        input_runtime_id: "new-runtime",
        input_runtime_epoch: 2
      }
    }));
    expect(latestRuntime?.liveTranscript).toBe("");
    expect(latestRuntime?.voiceState).toBe("ready");
  });

  it("跟进开关、任务状态和重连快照共同更新同一角色状态", async () => {
    mocks.fetchCurrentMaterialContext.mockResolvedValue(emptyMaterialContext(0));
    await act(async () => { root.render(<RuntimeProbe />); });
    const payload = { input_runtime_id: "runtime-a", input_runtime_epoch: 1, revision: 1,
      followup_state: "followup_cool", turn_state: "idle", session_id: "session-a" } as const;
    act(() => mocks.latestHandlers?.onEvent({ type: "interaction.state_changed", payload }));
    expect(latestRuntime?.companionStatus.label).toBe("等待唤醒");
    act(() => mocks.latestHandlers?.onEvent({ type: "interaction.state_changed",
      payload: { ...payload, revision: 2, followup_state: "followup_hot", source: "manual_toggle" } }));
    expect(latestRuntime?.companionStatus).toMatchObject({ label: "无需再唤醒", manualToggleRevision: 2 });
    act(() => mocks.latestHandlers?.onEvent({ type: "activities.snapshot_changed", payload: {
      runtime_id: "activities-a", revision: 1, recent: [], active: [{
        activity_id: "task-a", kind: "background_agent", type_label: "后台工作", status: "running",
        goal_summary: "整理资料", failure_summary: null, stage_code: "processing", stage_label: "处理中",
        started_at: "2026-10-10T12:00:00Z", updated_at: "2026-10-10T12:00:00Z", finished_at: null, can_cancel: true
      }] } }));
    expect(latestRuntime?.companionStatus).toMatchObject({ label: "执行任务中", wakeLabel: "无需再唤醒" });
    act(() => mocks.latestHandlers?.onStatusChange("connecting"));
    expect(latestRuntime?.companionStatus.state).toBe("syncing");
    await act(async () => {
      mocks.latestHandlers?.onStatusChange("connected");
      mocks.latestHandlers?.onEvent({ type: "interaction.state_changed",
        payload: { ...payload, revision: 0, turn_state: "speaking" } });
      await Promise.resolve();
    });
    expect(latestRuntime?.companionStatus.label).toBe("正在回复");
  });

  function RuntimeProbe({ showStatusRail = false }: { showStatusRail?: boolean }) {
    latestRuntime = useChatRuntime();
    return showStatusRail ? <ChatStatusRail
      companionName={latestRuntime.state.companionName}
      voiceState={latestRuntime.voiceState}
      companionStatus={latestRuntime.companionStatus}
      workspace={latestRuntime.workspace}
      llmAccountStatus={latestRuntime.workspace.llmAccountStatus}
    /> : null;
  }
});

function makeMaterialItem(
  overrides: Partial<MaterialContextSnapshot["turn_material_context"]["staged_materials"][number]> = {}
): MaterialContextSnapshot["turn_material_context"]["staged_materials"][number] {
  return {
    material_id: "material-test",
    turn_id: "turn-test",
    kind: "dragged_file",
    source: "drag_drop",
    display_name: "report.md",
    path: "G:\\docs\\report.md",
    node_kind: "file",
    created_at: "2026-06-27T10:00:00+08:00",
    media_class: "text",
    managed_asset_id: null,
    ...overrides
  };
}

function emptyMaterialContext(revision: number): MaterialContextSnapshot {
  return {
    revision,
    turn_material_context: { staged_materials: [], active: null },
    recent_materials: []
  };
}

function makeRecentMaterial(): MaterialContextSnapshot["recent_materials"][number] {
  return {
    display_name: "report.md",
    path: "G:\\docs\\report.md",
    material_kind: "dragged_file",
    node_kind: "file",
    media_class: "text",
    status: "available"
  };
}

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(nextResolve => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

function makeActivitySnapshot(
  revision: number,
  runtimeId: string,
  activityId: string
): import("../types/backendEvents").ActivitySnapshot {
  return {
    revision,
    runtime_id: runtimeId,
    active: [{
      activity_id: activityId,
      kind: "background_agent",
      type_label: "后台任务",
      status: "running",
      goal_summary: "测试任务",
      failure_summary: null,
      stage_code: "processing",
      stage_label: "处理中",
      started_at: "2026-09-24T10:00:00Z",
      updated_at: "2026-09-24T10:00:00Z",
      finished_at: null,
      can_cancel: true
    }],
    recent: []
  };
}

function makeReminderSnapshot(
  revision: number,
  runtimeId: string,
  reminderId: string,
  state: "scheduled" | "ringing"
): import("../types/backendEvents").TimedReminderSnapshot {
  return {
    revision,
    runtime_id: runtimeId,
    active: [{
      reminder_id: reminderId,
      content: "测试提醒",
      state,
      trigger_at: "2026-09-24T15:00:00Z",
      created_at: "2026-09-24T09:00:00Z",
      updated_at: "2026-09-24T09:00:00Z",
      finished_at: null,
      ring_count: state === "ringing" ? 1 : 0,
      next_ring_at: state === "ringing" ? null : "2026-09-24T15:00:00Z",
      finish_reason: null,
      can_modify: state === "scheduled",
      can_cancel: state === "scheduled",
      can_dismiss: state === "ringing"
    }],
    recent: []
  };
}
