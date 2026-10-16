// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MaterialContextSnapshot } from "../materials/materialTypes";
import type { ChatMaterialFeedback } from "./chatMaterialFeedback";
import { ChatApp } from "./ChatApp";
import type { ChatRuntime, StagedMaterialMutationResult } from "./useChatRuntime";

const useBootstrapStatusMock = vi.fn();
const useChatRuntimeMock = vi.fn();
const emptyTurnSubmissionState = {
  turnSubmissionsRevision: -1,
  finalizedSubmissionIds: new Set<string>()
};

vi.mock("../bootstrap/useBootstrapStatus", () => ({
  useBootstrapStatus: (...args: unknown[]) => useBootstrapStatusMock(...args)
}));

vi.mock("./useChatRuntime", () => ({
  useChatRuntime: (...args: unknown[]) => useChatRuntimeMock(...args)
}));

describe("ChatApp", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  let chatModeRequestListener: ((mode: "collapsed" | "expanded") => void) | null;
  let backgroundInteractionFocusListener: ((requestId: string) => void) | null;
  let removeChatModeRequestListener: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    useBootstrapStatusMock.mockReset();
    useChatRuntimeMock.mockReset();
    chatModeRequestListener = null;
    backgroundInteractionFocusListener = null;
    removeChatModeRequestListener = vi.fn();
    (window as Window & { companionChat?: unknown }).companionChat = {
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        }
      },
      submitTurn: vi.fn(),
      setChatMode: vi.fn(),
      startChatDrag: vi.fn(),
      moveChatDrag: vi.fn(),
      endChatDrag: vi.fn(),
      getChatMode: vi.fn(async () => "expanded" as const),
      onChatModeRequested: vi.fn((listener: (mode: "collapsed" | "expanded") => void) => {
        chatModeRequestListener = listener;
        return removeChatModeRequestListener;
      }),
      onFocusBackgroundAgentInteraction: vi.fn((listener: (requestId: string) => void) => {
        backgroundInteractionFocusListener = listener;
        return () => {
          backgroundInteractionFocusListener = null;
        };
      }),
      captureClipboardImageMaterial: vi.fn(async () => null)
    };
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
    delete (window as Window & { companionChat?: unknown }).companionChat;
  });

  it("聊天记录为空时展示克制空态，不读取历史记录", () => {
    renderReadyChat();

    expect(container.textContent).toContain("还没有聊天记录");
    expect(container.textContent).toContain("NearMate");
    expect(container.textContent).toContain("火花");
    expect(container.textContent).toContain("你的桌面陪伴");
    expect(container.textContent).toContain("等待唤醒");
  });

  it("collapsed 是隐藏态，不渲染独立聊天球", async () => {
    const getChatMode = vi.fn(async () => "collapsed" as const);
    (window as Window & { companionChat?: { getChatMode?: typeof getChatMode } }).companionChat!.getChatMode = getChatMode;
    renderReadyChat();
    await act(async () => { await Promise.resolve(); });
    expect(container.querySelector(".chat-dock-orb")).toBeNull();
    expect(container.querySelector(".chat-shell")).toBeNull();
  });

  it("展开态点击收起按钮会隐藏 Chat", () => {
    const chatBridge = (window as Window & { companionChat?: { setChatMode?: ReturnType<typeof vi.fn> } }).companionChat!;
    renderReadyChat();

    act(() => {
      (container.querySelector('button[aria-label="收起聊天窗口"]') as HTMLButtonElement).click();
    });

    expect(container.querySelector(".chat-dock-orb")).toBeNull();
    expect(container.querySelector(".chat-shell")).toBeFalsy();
    expect(chatBridge.setChatMode).toHaveBeenLastCalledWith("collapsed");
  });

  it("展开态点击关闭按钮同样隐藏 Chat", () => {
    const chatBridge = (window as Window & { companionChat?: { setChatMode?: ReturnType<typeof vi.fn> } }).companionChat!;
    renderReadyChat();

    act(() => {
      (container.querySelector('button[aria-label="关闭聊天窗口"]') as HTMLButtonElement).click();
    });

    expect(container.querySelector(".chat-dock-orb")).toBeNull();
    expect(container.querySelector(".chat-shell")).toBeFalsy();
    expect(chatBridge.setChatMode).toHaveBeenLastCalledWith("collapsed");
  });

  it("主进程请求 collapsed 时，展开态聊天窗会回到隐藏态且不聚焦输入框", () => {
    renderReadyChat();
    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
    const focusSpy = vi.spyOn(textarea, "focus");

    act(() => {
      chatModeRequestListener?.("collapsed");
    });

    expect(container.querySelector(".chat-dock-orb")).toBeNull();
    expect(container.querySelector(".chat-shell")).toBeFalsy();
    expect(focusSpy).not.toHaveBeenCalled();
  });

  it("桌宠气泡点击事件会展开聊天并定位对应待答卡片", () => {
    const requestId = "123e4567-e89b-42d3-a456-426614174000";
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView
    });
    renderReadyChat({
      state: {
        ...emptyTurnSubmissionState,
        messages: [{
          id: `agent-interaction-${requestId}`,
          role: "assistant",
          senderName: "火花",
          text: "",
          createdAt: 1710000000000,
          kind: "agent_interaction",
          agentInteraction: {
            requestId,
            activityId: "123e4567-e89b-42d3-a456-426614174001",
            kind: "permission",
            toolName: "Bash",
            inputData: { command: "ffmpeg -i input.wav output.mp3" },
            questions: [],
            actionSummary: "运行完成当前任务所需的命令",
            grantScope: "activity",
            status: "pending"
          }
        }],
        draftAssistantText: "",
        companionName: "火花"
      }
    }, { expand: false });

    act(() => {
      backgroundInteractionFocusListener?.(requestId);
    });

    const interactionCard = container.querySelector<HTMLElement>(
      `[data-background-interaction-id="${requestId}"]`
    );
    expect(container.querySelector(".chat-shell")).toBeTruthy();
    expect(interactionCard).toBeTruthy();
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "center" });
    expect(document.activeElement).toBe(interactionCard);
    delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
  });

  it("隐藏态不创建聊天拖拽入口", async () => {
    type DragBridge = {
      startChatDrag: ReturnType<typeof vi.fn>;
      moveChatDrag: ReturnType<typeof vi.fn>;
      endChatDrag: ReturnType<typeof vi.fn>;
    };
    const chatBridge = (window as Window & { companionChat?: DragBridge }).companionChat!;
    renderReadyChat({}, { expand: false });
    await act(async () => { await Promise.resolve(); });
    expect(container.querySelector(".chat-dock-orb")).toBeNull();
    expect(chatBridge.startChatDrag).not.toHaveBeenCalled();
  });

  it("按左右两侧展示用户和虚拟伴侣气泡，并使用各自头像", () => {
    renderReadyChat({
      state: {
        ...emptyTurnSubmissionState,
        messages: [
          {
            id: "user-1",
            role: "user",
            senderName: "master",
            text: "你好",
            createdAt: 1710000000000
          },
          {
            id: "assistant-1",
            role: "assistant",
            senderName: "火花",
            text: "我在。",
            createdAt: 1710000001000
          }
        ],
        draftAssistantText: "",
        companionName: "火花"
      }
    });

    const userMessage = container.querySelector('[data-role="user"]');
    const assistantMessage = container.querySelector('[data-role="assistant"]');

    expect(userMessage?.textContent).toContain("master");
    expect(userMessage?.textContent).toContain("你好");
    expect(userMessage?.querySelector("img")?.getAttribute("src")).toContain("user_icon");
    expect(userMessage?.querySelector("img")?.parentElement).toBe(
      userMessage?.querySelector(".chat-message-content")?.parentElement
    );
    expect(assistantMessage?.textContent).toContain("火花");
    expect(assistantMessage?.textContent).toContain("我在。");
    expect(assistantMessage?.querySelector("img")?.getAttribute("src")).toContain("icon");
  });

  it("消息变化时只滚动聊天记录容器，不依赖页面锚点滚动", () => {
    renderReadyChat({
      state: {
        ...emptyTurnSubmissionState,
        messages: [
          {
            id: "user-1",
            role: "user",
            senderName: "master",
            text: "第一条",
            createdAt: 1710000000000
          },
          {
            id: "assistant-1",
            role: "assistant",
            senderName: "火花",
            text: "第二条",
            createdAt: 1710000001000
          }
        ],
        draftAssistantText: "",
        companionName: "火花"
      }
    });

    const messageList = container.querySelector(".chat-message-list") as HTMLElement;

    Object.defineProperty(messageList, "scrollHeight", {
      configurable: true,
      value: 1234
    });

    renderReadyChat({
      state: {
        ...emptyTurnSubmissionState,
        messages: [
          {
            id: "user-1",
            role: "user",
            senderName: "master",
            text: "第一条",
            createdAt: 1710000000000
          },
          {
            id: "assistant-1",
            role: "assistant",
            senderName: "火花",
            text: "第二条",
            createdAt: 1710000001000
          },
          {
            id: "user-2",
            role: "user",
            senderName: "master",
            text: "第三条",
            createdAt: 1710000002000
          }
        ],
        draftAssistantText: "",
        companionName: "火花"
      }
    });

    expect(messageList.scrollTop).toBe(1234);
  });

  it("输入文本发送时会修剪首尾空白并调用 runtime.submitText", async () => {
    const submitText = vi.fn(async () => undefined);
    renderReadyChat({ submitText });

    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;
    const sendButton = Array.from(container.querySelectorAll("button")).find(
      button => button.textContent === "发送"
    ) as HTMLButtonElement;

    act(() => {
      textarea.value = "  明天天气怎么样  ";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    await act(async () => {
      sendButton.click();
      await Promise.resolve();
    });

    expect(submitText).toHaveBeenCalledWith("明天天气怎么样");
  });

  it("输入框按 Enter 会直接发送，Shift+Enter 仍保留给换行", async () => {
    const submitText = vi.fn(async () => undefined);
    renderReadyChat({ submitText });

    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;

    act(() => {
      textarea.value = "第一行";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      textarea.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
        shiftKey: true
      }));
    });
    expect(submitText).not.toHaveBeenCalled();

    const enterEvent = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true
    });

    await act(async () => {
      textarea.dispatchEvent(enterEvent);
      await Promise.resolve();
    });

    expect(enterEvent.defaultPrevented).toBe(true);
    expect(submitText).toHaveBeenCalledWith("第一行");
  });

  it("提交错误时展示错误文案且保留草稿", async () => {
    renderReadyChat({
      submitText: vi.fn(async () => { throw new Error("network"); }),
      textSubmitError: "文本发送失败，请稍后重试。"
    });

    expect(container.textContent).toContain("文本发送失败，请稍后重试。");
  });

  it("本轮 staged 材料有内容时展示本轮材料栏", () => {
    renderReadyChat({
      materialContext: {
        revision: 1,
        turn_material_context: {
          staged_materials: [
            makeMaterialItem({ material_id: "material-2", display_name: "a.md" }),
            makeMaterialItem({ material_id: "material-3", display_name: "b.md" })
          ],
          active: {
            context_id: "turn-context-1",
            turn_id: "turn-1",
            created_at: "2026-07-06T10:00:00+08:00",
            attachments: [makeMaterialItem({ material_id: "material-active", display_name: "本轮材料.md" })]
          }
        },
        recent_materials: []
      }
    });

    expect(container.textContent).toContain("本轮材料");
    expect(container.textContent).toContain("2 项");
    expect(container.textContent).toContain("a.md");
    expect(container.textContent).toContain("b.md");
    expect(container.textContent).not.toContain("当前环境快照");
    expect(container.textContent).not.toContain("本轮材料.md");
    expect(container.querySelector(".material-shelf-strip")).toBeTruthy();
  });

  it("点击材料缩略图删除按钮时调用 runtime.removeMaterialItem", async () => {
    const removeMaterialItem = vi.fn<(_: string) => Promise<void>>().mockResolvedValue(undefined);
    renderReadyChat({
      removeMaterialItem,
      materialContext: {
        revision: 1,
        turn_material_context: {
          staged_materials: [makeMaterialItem({ material_id: "material-2", display_name: "a.md" })],
          active: null
        },
        recent_materials: []
      }
    });

    const deleteButton = container.querySelector('button[aria-label="删除 a.md"]') as HTMLButtonElement;
    await act(async () => {
      deleteButton.click();
      await Promise.resolve();
    });

    expect(removeMaterialItem).toHaveBeenCalledWith("material-2");
  });

  it("近期材料使用独立区域展示，并调用 runtime 刷新和忘记动作", async () => {
    const refreshMaterialContext = vi.fn(async () => undefined);
    const forgetRecentMaterial = vi.fn(async () => undefined);
    renderReadyChat({
      refreshMaterialContext,
      forgetRecentMaterial,
      materialContext: {
        revision: 2,
        turn_material_context: { staged_materials: [], active: null },
        recent_materials: [{
          display_name: "project",
          path: "G:\\workspace\\project",
          material_kind: "dragged_folder",
          node_kind: "directory",
          media_class: "mixed_or_document",
          status: "available"
        }]
      }
    });

    expect(container.textContent).toContain("NearMate 当前记得");
    expect(container.textContent).toContain("project");
    expect(container.textContent).toContain("目录 · 可用");
    expect(container.querySelector(".material-shelf")).toBeNull();
    await act(async () => {
      (Array.from(container.querySelectorAll("button"))
        .find(button => button.textContent === "刷新") as HTMLButtonElement).click();
      (container.querySelector('button[aria-label="忘记 project"]') as HTMLButtonElement).click();
      await Promise.resolve();
    });

    expect(refreshMaterialContext).toHaveBeenCalledOnce();
    expect(forgetRecentMaterial).toHaveBeenCalledWith("G:\\workspace\\project");
  });

  it("拖拽文件进入聊天窗口时只追加 staged 材料，不提交文本 turn", async () => {
    const submitText = vi.fn(async () => undefined);
    const addStagedMaterials = vi.fn<(_: unknown[]) => Promise<StagedMaterialMutationResult>>()
      .mockResolvedValue(makeStagedMutationResult({
        feedback: { kind: "warning", text: "已添加 1 项，1 项格式不支持，已过滤" }
      }));
    (window as Window & { companionChat?: { getPathForFile?(file: File): string } }).companionChat!.getPathForFile = vi.fn(
      () => "G:\\docs\\report.md"
    );
    renderReadyChat({ submitText, addStagedMaterials });
    const chatHistory = container.querySelector(".chat-history-surface") as HTMLElement;
    const file = new File(["hello"], "report.md", { type: "text/markdown" });
    const dropEvent = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dropEvent, "dataTransfer", {
      value: {
        files: [file]
      }
    });

    await act(async () => {
      chatHistory.dispatchEvent(dropEvent);
      await Promise.resolve();
    });

    expect(addStagedMaterials).toHaveBeenCalledTimes(1);
    expect(addStagedMaterials).toHaveBeenCalledWith([
      {
        kind: "dragged_file",
        source: "drag_drop",
        display_name: "report.md",
        path: "G:\\docs\\report.md"
      }
    ]);
    expect(submitText).not.toHaveBeenCalled();
    expect(container.textContent).toContain("已添加 1 项，1 项格式不支持，已过滤");
  });

  it("粘贴图片时会追加 staged 材料且不提交文本 turn", async () => {
    const submitText = vi.fn(async () => undefined);
    const managedAssetId = "asset-clipboard-image";
    const addStagedMaterials = vi.fn<(_: unknown[]) => Promise<StagedMaterialMutationResult>>()
      .mockResolvedValue(makeStagedMutationResult({
        stagedMaterials: [makeMaterialItem({
          kind: "clipboard_image",
          managed_asset_id: managedAssetId
        })]
      }));
    const chatBridge = (window as Window & {
      companionChat?: {
        captureClipboardImageMaterial?(): Promise<unknown>;
        getMaterialThumbnail?(path: string): Promise<string | null>;
      };
    }).companionChat!;
    chatBridge.captureClipboardImageMaterial = vi.fn(
      async () => ({
        kind: "clipboard_image",
        source: "clipboard",
        display_name: "剪贴板图片",
        path: "G:\\Temp\\clipboard.png",
        managed_asset_id: managedAssetId
      })
    );
    chatBridge.getMaterialThumbnail = vi.fn(async () => "data:image/png;base64,preview");
    renderReadyChat({ submitText, addStagedMaterials });
    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;

    await act(async () => {
      textarea.dispatchEvent(new Event("paste", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(addStagedMaterials).toHaveBeenCalledWith([
      {
        kind: "clipboard_image",
        source: "clipboard",
        display_name: "剪贴板图片",
        path: "G:\\Temp\\clipboard.png",
        managed_asset_id: managedAssetId
      }
    ]);
    expect(chatBridge.getMaterialThumbnail).not.toHaveBeenCalled();
    expect(submitText).not.toHaveBeenCalled();
  });

  it("粘贴图片追加本轮待提交材料失败时展示错误", async () => {
    const chatBridge = (window as Window & {
      companionChat?: {
        captureClipboardImageMaterial?(): Promise<unknown>;
        getMaterialThumbnail?(path: string): Promise<string | null>;
      };
    }).companionChat!;
    chatBridge.captureClipboardImageMaterial = vi.fn(async () => ({
      kind: "clipboard_image",
      source: "clipboard",
      display_name: "剪贴板图片",
      path: "G:\\Temp\\clipboard.png",
      managed_asset_id: "asset-clipboard-image"
    }));
    chatBridge.getMaterialThumbnail = vi.fn(async () => null);
    renderReadyChat({
      addStagedMaterials: vi.fn<(_: unknown[]) => Promise<StagedMaterialMutationResult>>()
        .mockRejectedValue(new Error("append failed"))
    });
    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;

    await act(async () => {
      textarea.dispatchEvent(new Event("paste", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("材料添加失败，请稍后重试。");
  });

  it("粘贴图片未出现在 Backend 权威快照时按事务失败处理", async () => {
    const chatBridge = (window as Window & {
      companionChat?: { captureClipboardImageMaterial?(): Promise<unknown> };
    }).companionChat!;
    chatBridge.captureClipboardImageMaterial = vi.fn(async () => ({
      kind: "clipboard_image",
      source: "clipboard",
      display_name: "剪贴板图片",
      path: "G:\\Temp\\clipboard.png",
      managed_asset_id: "asset-not-retained"
    }));
    renderReadyChat({
      addStagedMaterials: vi.fn<(_: unknown[]) => Promise<StagedMaterialMutationResult>>()
        .mockResolvedValue(makeStagedMutationResult())
    });
    const textarea = container.querySelector("textarea") as HTMLTextAreaElement;

    await act(async () => {
      textarea.dispatchEvent(new Event("paste", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("粘贴处理失败，请重试。");
    expect(textarea.readOnly).toBe(false);
  });

  it("拖拽文件无法解析路径时展示错误且不提交材料", async () => {
    const submitText = vi.fn(async () => undefined);
    (window as Window & { companionChat?: { getPathForFile?(file: File): string } }).companionChat!.getPathForFile = vi.fn(
      () => ""
    );
    renderReadyChat({ submitText });
    const chatShell = container.querySelector(".chat-shell") as HTMLElement;
    const file = new File(["hello"], "report.md", { type: "text/markdown" });
    const dropEvent = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
    Object.defineProperty(dropEvent, "dataTransfer", {
      value: {
        files: [file]
      }
    });

    await act(async () => {
      chatShell.dispatchEvent(dropEvent);
      await Promise.resolve();
    });

    expect(submitText).not.toHaveBeenCalled();
    expect(container.textContent).toContain("没有拿到文件路径");
  });

  it("拖拽进入聊天窗口后即使释放到窗口外，也会收起投递遮罩", () => {
    renderReadyChat();
    const chatShell = container.querySelector(".chat-shell") as HTMLElement;

    act(() => {
      chatShell.dispatchEvent(new Event("dragover", { bubbles: true, cancelable: true }));
    });

    expect(container.textContent).toContain("松开以添加到本轮材料");

    act(() => {
      window.dispatchEvent(new Event("dragend"));
    });

    expect(container.textContent).not.toContain("松开以添加到本轮材料");
  });

  function renderReadyChat(
    runtimeOverrides: Partial<TestChatRuntime> = {},
    options: { expand?: boolean } = {}
  ) {
    const runtime: TestChatRuntime = {
      state: {
        messages: [],
        draftAssistantText: "",
        companionName: "火花",
        turnSubmissionsRevision: -1,
        finalizedSubmissionIds: new Set<string>()
      },
      textSubmitError: null,
      materialMutationError: null,
      submitText: vi.fn(async () => undefined),
      addStagedMaterials: vi.fn<(_: unknown[]) => Promise<StagedMaterialMutationResult>>()
        .mockResolvedValue(makeStagedMutationResult()),
      removeMaterialItem: vi.fn<(_: string) => Promise<void>>().mockResolvedValue(undefined),
      refreshMaterialContext: vi.fn(async () => undefined),
      forgetRecentMaterial: vi.fn(async () => undefined),
      editTurnSubmission: vi.fn(async () => undefined),
      deleteTurnSubmission: vi.fn(async () => undefined),
      resumeTurnSubmission: vi.fn(async () => undefined),
      retryTurnSubmission: vi.fn(async () => undefined),
      voiceState: "ready",
      companionStatus: { state: "waiting_wake", label: "等待唤醒", description: "叫我一声，再开始语音聊天。",
        wakeMode: "wake_required", wakeLabel: "需要唤醒", manualToggleRevision: null },
      liveTranscript: "",
      workspace: {
        activitySnapshot: { revision: 0, runtime_id: "", active: [], recent: [] },
        reminderSnapshot: { revision: 0, runtime_id: "", active: [], recent: [] },
        requestingActivityIds: new Set<string>(),
        requestingReminderIds: new Set<string>(),
        errors: { activity: null, reminder: null },
        runtimeRestartMessage: null,
        enabledSkills: [],
        requestCancelActivity: vi.fn(async () => undefined),
        requestUpdateReminder: vi.fn(async () => undefined),
        requestCancelReminder: vi.fn(async () => undefined),
        requestDismissReminders: vi.fn(async () => undefined),
        openActivityDetail: vi.fn()
      },
      materialContext: null,
      ...runtimeOverrides,
      submitWorkInteractionAction: runtimeOverrides.submitWorkInteractionAction
        ?? vi.fn(async () => undefined)
    };

    useBootstrapStatusMock.mockReturnValue({
      status: "ready",
      reason: null,
      retry: vi.fn()
    });
    useChatRuntimeMock.mockReturnValue(runtime);

    act(() => {
      root.render(<ChatApp />);
    });

    if (options.expand ?? true) {
      act(() => {
        (container.querySelector(".chat-dock-orb") as HTMLButtonElement | null)?.click();
      });
    }

    return runtime;
  }
});

type TestChatRuntime = ChatRuntime & {
  materialContext: MaterialContextSnapshot | null;
};

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
    ...overrides
  };
}

function makeStagedMutationResult(
  input: {
    feedback?: ChatMaterialFeedback | null;
    stagedMaterials?: MaterialContextSnapshot["turn_material_context"]["staged_materials"];
  } = {}
): StagedMaterialMutationResult {
  return {
    feedback: input.feedback ?? null,
    materialContext: {
      revision: 1,
      turn_material_context: {
        staged_materials: input.stagedMaterials ?? [],
        active: null
      },
      recent_materials: []
    }
  };
}
