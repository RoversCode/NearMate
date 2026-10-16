// @vitest-environment jsdom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChatMessageList } from "./ChatMessageList";
import type { ChatState } from "./chatReducer";

const emptyTurnSubmissionState = {
  turnSubmissionsRevision: -1,
  finalizedSubmissionIds: new Set<string>()
};

describe("ChatMessageList taskResult card", () => {
  it("用户消息以可折叠卡展示选中文档引用", () => {
    const { container } = render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          messages: [{
            id: "user-1",
            role: "user",
            senderName: "master",
            text: "这里是什么意思？",
            createdAt: 0,
            references: [{ displayName: "选中文本.txt", text: "被引用的完整短文本" }]
          }],
          draftAssistantText: "",
          companionName: "小练"
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
      />
    );

    expect(screen.getByText("选中文本.txt")).toBeTruthy();
    expect(screen.getByText("被引用的完整短文本")).toBeTruthy();
    expect(container.querySelector("details")).not.toBeNull();
  });
  it("渲染 task_result 结果卡并提供复制按钮", () => {
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn()
      }
    });
    const state: ChatState = {
      ...emptyTurnSubmissionState,
      companionName: "小练",
      draftAssistantText: "",
      messages: [
        {
          id: "taskResult-1",
          role: "assistant",
          kind: "task_result",
          senderName: "小练",
          text: "",
          createdAt: 10,
          taskResult: {
            activityId: "taskResult-1",
            status: "completed",
            goal: "总结这些材料",
            contentText: "这是整理好的正文。",
            sourceMaterials: ["report.pdf"],
            outputPaths: []
          }
        }
      ]
    };

    render(
      <ChatMessageList
        state={state}
        companionAvatarUrl="data:image/png;base64,a"
        userAvatarUrl="data:image/png;base64,b"
      />
    );

    expect(screen.getByText("总结这些材料")).toBeTruthy();
    expect(screen.getByText("这是整理好的正文。")).toBeTruthy();
    expect(screen.getByText("report.pdf")).toBeTruthy();
    expect(screen.getByRole("button", { name: "复制结果" })).toBeTruthy();
  });

  it("无材料创作结果不渲染来源区域", () => {
    const state: ChatState = {
      ...emptyTurnSubmissionState,
      companionName: "小练",
      draftAssistantText: "",
      messages: [
        {
          id: "taskResult-poem",
          role: "assistant",
          kind: "task_result",
          senderName: "小练",
          text: "月照花间露，风来竹外声。",
          createdAt: 10,
          taskResult: {
            activityId: "taskResult-poem",
            status: "completed",
            goal: "写一首关于春夜的古诗",
            contentText: "月照花间露，风来竹外声。",
            sourceMaterials: [],
            outputPaths: []
          }
        }
      ]
    };

    const { container } = render(
      <ChatMessageList
        state={state}
        companionAvatarUrl="data:image/png;base64,a"
        userAvatarUrl="data:image/png;base64,b"
      />
    );

    expect(screen.getByText("写一首关于春夜的古诗")).toBeTruthy();
    expect(container.querySelector(".chat-task-result-sources")).toBeNull();
  });

  it("交付文件通过受控回调打开所在文件夹", () => {
    const onOpenOutputPath = vi.fn(async () => undefined);
    render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: "taskResult-file",
            role: "assistant",
            kind: "task_result",
            senderName: "小练",
            text: "已生成文件。",
            createdAt: 10,
            taskResult: {
              activityId: "taskResult-file",
              status: "completed",
              goal: "生成报告",
              contentText: "已生成文件。",
              sourceMaterials: [],
              outputPaths: ["G:\\NearMate Workspace\\report.md"]
            }
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
        onOpenOutputPath={onOpenOutputPath}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "打开 report.md 所在文件夹" }));
    expect(onOpenOutputPath).toHaveBeenCalledWith("G:\\NearMate Workspace\\report.md");
  });

  it("权限卡把允许决定提交给回答端口并在 resolved 后收口", async () => {
    const onAnswerInteraction = vi.fn(async () => undefined);
    const interaction = {
      requestId: "permission-a",
      activityId: "activity-a",
      kind: "permission" as const,
      toolName: "Bash",
      inputData: { command: "ffmpeg -i input.wav output.mp3" },
      questions: [],
      actionSummary: "运行完成当前任务所需的命令",
      grantScope: "activity" as const,
      status: "pending" as const
    };
    const { rerender } = render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: "agent-interaction-permission-a",
            role: "assistant",
            kind: "agent_interaction",
            senderName: "小练",
            text: "",
            createdAt: 10,
            agentInteraction: interaction
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
        onAnswerInteraction={onAnswerInteraction}
      />
    );

    expect(screen.getByText("运行完成当前任务所需的命令")).toBeTruthy();
    const operationDetails = screen.getByText("查看操作详情").closest("details");
    expect(operationDetails?.open).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "授权本次任务" }));
    await waitFor(() => expect(onAnswerInteraction).toHaveBeenCalledWith({
      requestId: "permission-a",
      kind: "permission",
      outcome: "allow"
    }));

    rerender(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: "agent-interaction-permission-a",
            role: "assistant",
            kind: "agent_interaction",
            senderName: "小练",
            text: "",
            createdAt: 10,
            agentInteraction: { ...interaction, status: "resolved", outcome: "allowed" }
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
        onAnswerInteraction={onAnswerInteraction}
      />
    );
    expect(screen.getByText("本次任务已授权")).toBeTruthy();
  });

  it("资料问题卡提交问题正文到答案映射", async () => {
    const onAnswerInteraction = vi.fn(async () => undefined);
    render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: "agent-interaction-question-a",
            role: "assistant",
            kind: "agent_interaction",
            senderName: "小练",
            text: "",
            createdAt: 10,
            agentInteraction: {
              requestId: "question-a",
              activityId: "activity-a",
              kind: "question",
              toolName: "AskUserQuestion",
              inputData: {},
              questions: [{
                header: "格式",
                question: "输出格式？",
                options: [{ label: "MP3", description: "兼容性更好" }],
                multiSelect: false
              }],
              status: "pending"
            }
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
        onAnswerInteraction={onAnswerInteraction}
      />
    );

    fireEvent.click(screen.getByRole("radio", { name: /MP3/ }));
    fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
    await waitFor(() => expect(onAnswerInteraction).toHaveBeenCalledWith({
      requestId: "question-a",
      kind: "question",
      messageText: "我对当前工作的补充回答：\n- 输出格式？：MP3"
    }));
  });

  it("用户正文中的长 URL 截短显示，并通过 preload 打开完整目标", () => {
    const openExternalUrl = vi.fn(async () => undefined);
    const url = "https://example.com/reports/2026/very-long-path-name?source=desktop&section=summary";
    (window as Window & { companionChat?: unknown }).companionChat = {
      openExternalUrl,
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        }
      }
    };

    render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: "user-url",
            role: "user",
            senderName: "master",
            text: `请总结 ${url}。`,
            createdAt: 10
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
      />
    );

    const link = screen.getByRole("link") as HTMLButtonElement;
    expect(link.getAttribute("href")).toBeNull();
    expect(link.title).toBe(url);
    expect(link.textContent).toContain("…");
    expect(link.textContent).not.toBe(url);
    fireEvent.click(link);
    expect(openExternalUrl).toHaveBeenCalledWith(url);
    delete (window as Window & { companionChat?: unknown }).companionChat;
  });

  it("链接边界保留 URL 自身括号并排除外部反引号", () => {
    const openExternalUrl = vi.fn(async () => undefined);
    (window as Window & { companionChat?: unknown }).companionChat = {
      openExternalUrl,
      reportDiagnostic: vi.fn(),
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        }
      }
    };
    const pairedUrl = "https://en.wikipedia.org/wiki/Foo_(bar)";

    render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: "user-url-brackets",
            role: "user",
            senderName: "master",
            text: `查看 ${pairedUrl}，再看 https://example.com/report\`后续`,
            createdAt: 10
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
      />
    );

    const pairedLink = screen.getByTitle(pairedUrl);
    expect(screen.getByTitle("https://example.com/report")).toBeTruthy();
    fireEvent.click(pairedLink);
    expect(openExternalUrl).toHaveBeenCalledWith(pairedUrl);
    delete (window as Window & { companionChat?: unknown }).companionChat;
  });

  it("paused 待发文本支持原位编辑、恢复和删除", async () => {
    const submissionId = "00000000-0000-4000-8000-000000000211";
    const onEdit = vi.fn(async () => undefined);
    const onDelete = vi.fn(async () => undefined);
    const onResume = vi.fn(async () => undefined);
    render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: `turn-submission-${submissionId}`,
            role: "user",
            senderName: "master",
            text: "原正文",
            createdAt: 10,
            submissionId,
            submissionState: "paused",
            submissionError: null,
            submissionAcknowledged: true
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
        onEditTurnSubmission={onEdit}
        onDeleteTurnSubmission={onDelete}
        onResumeTurnSubmission={onResume}
      />
    );

    expect(screen.getByText("语音输入优先，已暂停")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑待发文本" }));
    fireEvent.change(screen.getByRole("textbox", { name: "编辑待发文本" }), {
      target: { value: "修改后的正文" }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(onEdit).toHaveBeenCalledWith(submissionId, "修改后的正文"));

    fireEvent.click(screen.getByRole("button", { name: "恢复发送" }));
    await waitFor(() => expect(onResume).toHaveBeenCalledWith(submissionId));
    fireEvent.click(screen.getByRole("button", { name: "删除待发文本" }));
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith(submissionId));
  });

  it("failed 待发文本展示 Backend 错误并提供重试", async () => {
    const submissionId = "00000000-0000-4000-8000-000000000212";
    const onRetry = vi.fn(async () => undefined);
    render(
      <ChatMessageList
        state={{
          ...emptyTurnSubmissionState,
          companionName: "小练",
          draftAssistantText: "",
          messages: [{
            id: `turn-submission-${submissionId}`,
            role: "user",
            senderName: "master",
            text: "失败正文",
            createdAt: 10,
            submissionId,
            submissionState: "failed",
            submissionError: "现场环境读取失败，请重试。",
            submissionAcknowledged: true
          }]
        }}
        companionAvatarUrl="companion.png"
        userAvatarUrl="user.png"
        onRetryTurnSubmission={onRetry}
      />
    );

    expect(screen.getByText("现场环境读取失败，请重试。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试发送" }));
    await waitFor(() => expect(onRetry).toHaveBeenCalledWith(submissionId));
  });
});
