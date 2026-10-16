import { describe, expect, it } from "vitest";
import type { BackendEvent, ReminderSettings } from "../types/events";
import { parseBackendEvent } from "../types/events";
import {
  createInitialChatState,
  reduceChatState,
  type ChatState
} from "./chatReducer";

const settingsFixture: ReminderSettings = {
  show_speech_bubbles: true,
  followup_toggle_shortcut_enabled: true,
  followup_toggle_shortcut: "CommandOrControl+Alt+Space",
  selected_live2d_model_id: "Haru"
};

describe("chatReducer", () => {
  it("Self-Reflection 临时状态不进入聊天历史", () => {
    const state = createInitialChatState(settingsFixture);
    const nextState = dispatch(state, {
      type: "assistant.self_reflection_status_changed",
      payload: {
        request_id: "reflection-request-1",
        session_id: "session-reflection",
        generation_id: "generation-reflection-1",
        active: true
      }
    });

    expect(nextState).toBe(state);
    expect(nextState.messages).toEqual([]);
  });

  it("启动时聊天记录为空，并使用本地聊天显示名", () => {
    const state = createInitialChatState(settingsFixture);

    expect(state.messages).toEqual([]);
    expect(state.companionName).toBe("当前角色");
  });

  it("助手输出不可用时保存持续错误，恢复事件会清除错误", () => {
    let state = createInitialChatState(settingsFixture);

    state = dispatch(state, {
      type: "assistant.output_status_changed",
      payload: {
        available: false,
        code: "output_unavailable",
        message: "助手输出暂时不可用，请重新连接后再试。"
      }
    });

    expect(state.assistantOutputError).toBe("助手输出暂时不可用，请重新连接后再试。");

    state = dispatch(state, {
      type: "assistant.output_status_changed",
      payload: { available: true, code: null, message: null }
    });

    expect(state.assistantOutputError).toBeNull();
  });

  it("用户 final transcript 会追加 master 消息", () => {
    const state = dispatch(createInitialChatState(settingsFixture), {
      type: "user.final_transcript",
      payload: {
        text: "明天天气怎么样"
      }
    });

    expect(state.messages).toMatchObject([
      {
        role: "user",
        senderName: "master",
        text: "明天天气怎么样"
      }
    ]);
  });

  it("唤醒回复进入聊天流，并抵御重复唤醒控制帧", () => {
    const wakeEvent: BackendEvent = {
      type: "wake.detected",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000001",
        input_runtime_epoch: 1,
        session_id: "session-voice",
        text: "我在，正在听。"
      }
    };

    const first = dispatch(createInitialChatState(settingsFixture), wakeEvent);
    const second = dispatch(first, wakeEvent);

    expect(first.messages).toHaveLength(1);
    expect(first.messages[0]).toMatchObject({
      role: "assistant",
      senderName: "当前角色",
      text: "我在，正在听。"
    });
    expect(second.messages).toHaveLength(1);
  });

  it("episode 输入和辅助回应只在闭合事件后按序原子进入稳定聊天", () => {
    const submissionId = "00000000-0000-4000-8000-000000000051";
    const optimistic = reduceChatState(createInitialChatState(settingsFixture), {
      type: "turn_submission_optimistically_added",
      submissionId,
      messageText: "先别急"
    });
    const diverted = dispatch(optimistic, {
      type: "user.final_transcript",
      payload: {
        session_id: "desktop-text",
        turn_id: null,
        request_id: `text-input-${submissionId}`,
        submission_id: submissionId,
        history_disposition: "unattended_episode",
        episode_id: "episode-1",
        text: "先别急"
      }
    });
    const streaming = dispatch(diverted, {
      type: "assistant.text_chunk",
      payload: {
        request_id: "reply-1",
        session_id: "desktop-text",
        generation_id: "generation-reply-1",
        request_source: "self_reflection_interruption_reply",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        text: "我听到了。"
      }
    });
    const auxiliaryFinal = dispatch(streaming, {
      type: "assistant.text_final",
      payload: {
        request_id: "reply-1",
        session_id: "desktop-text",
        generation_id: "generation-reply-1",
        request_source: "self_reflection_interruption_reply",
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        text: "我听到了。"
      }
    });
    const committedEvent = {
      type: "self_reflection.episode_committed",
      payload: {
        self_reflection_id: "self-reflection-1",
        episode_id: "episode-1",
        runtime_epoch: 7,
        revision: 4,
        actionable: false,
        history_block: [
          {
            role: "user",
            content: "先别急",
            occurred_at: "2026-08-16T15:00:00+08:00",
            episode_role: "input",
            source_identity: `text-input-${submissionId}`,
            reply_settlement: null
          },
          {
            role: "assistant",
            content: "我听到了。",
            occurred_at: "2026-08-16T15:00:01+08:00",
            episode_role: "interruption_reply",
            source_identity: "reply-1",
            reply_settlement: "completed"
          },
          {
            role: "assistant",
            content: "这些内容当时没有处理。",
            occurred_at: "2026-08-16T15:00:02+08:00",
            episode_role: "closure",
            source_identity: null,
            reply_settlement: null
          }
        ]
      }
    } as const;
    const committed = dispatch(auxiliaryFinal, committedEvent);
    const duplicate = dispatch(committed, committedEvent);

    expect(diverted.messages).toEqual([]);
    expect(streaming.messages).toEqual([]);
    expect(streaming.draftAssistantText).toBe("我听到了。");
    expect(auxiliaryFinal.messages).toEqual([]);
    expect(auxiliaryFinal.draftAssistantText).toBe("");
    expect(committed.messages.map(message => [message.role, message.text])).toEqual([
      ["user", "先别急"],
      ["assistant", "我听到了。"],
      ["assistant", "这些内容当时没有处理。"]
    ]);
    expect(duplicate).toBe(committed);
  });

  it("episode 闭合历史按 runtime epoch 和最高 revision 幂等替换", () => {
    const event = (
      runtimeEpoch: number,
      revision: number,
      inputText: string
    ) => ({
      type: "self_reflection.episode_committed" as const,
      payload: {
        self_reflection_id: `self-reflection-${runtimeEpoch}`,
        episode_id: "episode-shared",
        runtime_epoch: runtimeEpoch,
        revision,
        actionable: false as const,
        history_block: [
          {
            role: "user" as const,
            content: inputText,
            occurred_at: "2026-08-16T15:00:00+08:00",
            episode_role: "input" as const,
            source_identity: `input-${runtimeEpoch}`,
            reply_settlement: null
          },
          {
            role: "assistant" as const,
            content: "这些内容当时没有处理。",
            occurred_at: "2026-08-16T15:00:01+08:00",
            episode_role: "closure" as const,
            source_identity: null,
            reply_settlement: null
          }
        ]
      }
    });
    const initial = createInitialChatState(settingsFixture);
    const lowRevision = dispatch(initial, event(7, 1, "低版本正文"));
    const duplicate = dispatch(lowRevision, event(7, 1, "不能覆盖"));
    const highRevision = dispatch(lowRevision, event(7, 2, "权威正文"));
    const nextRuntime = dispatch(highRevision, event(8, 1, "新世代正文"));

    expect(duplicate).toBe(lowRevision);
    expect(highRevision.messages.map(message => message.text)).toEqual([
      "权威正文",
      "这些内容当时没有处理。"
    ]);
    expect(
      highRevision.committedUnattendedEpisodeRevisions?.get(
        "7:episode-shared"
      )
    ).toBe(2);
    expect(nextRuntime.messages.map(message => message.text)).toEqual([
      "权威正文",
      "这些内容当时没有处理。",
      "新世代正文",
      "这些内容当时没有处理。"
    ]);
  });

  it("旧 Runtime 的 Voice Final 经 WebSocket JSON 解析后仍进入 Chat", () => {
    const rawMessage = JSON.stringify({
      type: "user.final_transcript",
      payload: {
        input_runtime_id: "input-runtime-00000000-0000-4000-8000-000000000009",
        input_runtime_epoch: 9,
        session_id: "session-voice",
        turn_id: "turn-9",
        turn_sequence: 9,
        history_disposition: "ordinary_turn",
        text: "不能丢失的真实用户消息"
      }
    });
    const parsed = parseBackendEvent(JSON.parse(rawMessage));
    if (!parsed.ok) {
      throw new Error(`Voice Final 解析失败：${parsed.reasonCode}`);
    }

    const state = dispatch(createInitialChatState(settingsFixture), parsed.event);

    expect(state.messages).toMatchObject([{
      role: "user",
      text: "不能丢失的真实用户消息"
    }]);
  });

  it("用户消息保留本轮选中文档引用卡", () => {
    const state = dispatch(createInitialChatState(settingsFixture), {
      type: "user.final_transcript",
      payload: {
        text: "这里是什么意思？",
        references: [{ display_name: "选中文本.txt", text: "被引用的完整短文本" }]
      }
    });

    expect(state.messages[0].references).toEqual([
      { displayName: "选中文本.txt", text: "被引用的完整短文本" }
    ]);
  });

  it("助手 chunk 只进入草稿，final 后成为伴侣消息并清空草稿", () => {
    const initialState = createInitialChatState(settingsFixture);
    const chunkedState = dispatch(initialState, {
      type: "assistant.text_chunk",
      payload: {
        text: "我查"
      }
    });

    expect(chunkedState.draftAssistantText).toBe("我查");
    expect(chunkedState.messages).toEqual([]);

    const finalState = dispatch(chunkedState, {
      type: "assistant.text_final",
      payload: {
        text: "我查到明天会下雨。"
      }
    });

    expect(finalState.draftAssistantText).toBe("");
    expect(finalState.messages).toMatchObject([
      {
        role: "assistant",
        senderName: "当前角色",
        text: "我查到明天会下雨。"
      }
    ]);
  });

  it("助手分段完成会收起当前草稿，但不会阻止同一 generation 继续产生下一条消息", () => {
    const prefixState = dispatch(createInitialChatState(settingsFixture), {
      type: "assistant.text_chunk",
      payload: {
        generation_id: "generation-a",
        text: "我先去处理。"
      }
    });
    const segmentedState = dispatch(prefixState, {
      type: "assistant.text_segment_final",
      payload: {
        generation_id: "generation-a",
        text: "我先去处理。"
      }
    });
    const followupDraftState = dispatch(segmentedState, {
      type: "assistant.text_chunk",
      payload: {
        generation_id: "generation-a",
        text: "不过后台工作还没有配置。"
      }
    });
    const finalState = dispatch(followupDraftState, {
      type: "assistant.text_final",
      payload: {
        generation_id: "generation-a",
        text: "不过后台工作还没有配置。"
      }
    });

    expect(finalState.draftAssistantText).toBe("");
    expect(finalState.messages.map(message => message.text)).toEqual([
      "我先去处理。",
      "不过后台工作还没有配置。"
    ]);
  });

  it("assistant.request_failed 在用户消息后追加助手失败提示并清空草稿", () => {
    const userState = dispatch(createInitialChatState(settingsFixture), {
      type: "user.final_transcript",
      payload: { request_id: "text-input-1", text: "请回答" }
    });
    const withDraft = { ...userState, draftAssistantText: "不应保留" };
    const failureEvent: BackendEvent = {
      type: "assistant.request_failed",
      payload: {
        request_id: "text-input-1",
        code: "request_start_failed",
        message: "消息已经发送，但我现在没能开始回复，请稍后再试。"
      }
    };

    const failedState = dispatch(withDraft, failureEvent);
    expect(failedState.draftAssistantText).toBe("");
    expect(failedState.messages).toMatchObject([
      { role: "user", requestId: "text-input-1", text: "请回答" },
      {
        role: "assistant",
        senderName: "当前角色",
        text: "消息已经发送，但我现在没能开始回复，请稍后再试。"
      }
    ]);
  });

  it("延迟失败会插入原用户消息之后而不是错误追加到新轮次末尾", () => {
    const firstUserState = dispatch(createInitialChatState(settingsFixture), {
      type: "user.final_transcript",
      payload: { request_id: "text-input-1", text: "第一条问题" }
    });
    const secondUserState = dispatch(firstUserState, {
      type: "user.final_transcript",
      payload: { request_id: "text-input-2", text: "第二条问题" }
    });

    const failedState = dispatch(secondUserState, {
      type: "assistant.request_failed",
      payload: {
        request_id: "text-input-1",
        code: "request_start_failed",
        message: "第一条消息未能启动回复。"
      }
    });

    expect(failedState.messages).toMatchObject([
      { role: "user", requestId: "text-input-1", text: "第一条问题" },
      { role: "assistant", text: "第一条消息未能启动回复。" },
      { role: "user", requestId: "text-input-2", text: "第二条问题" }
    ]);
  });

  it("重复的请求失败事件不会生成多条助手提示", () => {
    const userState = dispatch(createInitialChatState(settingsFixture), {
      type: "user.final_transcript",
      payload: { request_id: "text-input-1", text: "请回答" }
    });
    const failureEvent: BackendEvent = {
      type: "assistant.request_failed",
      payload: {
        request_id: "text-input-1",
        code: "request_start_failed",
        message: "消息已经发送，但我现在没能开始回复，请稍后再试。"
      }
    };

    const once = dispatch(userState, failureEvent);
    const twice = dispatch(once, failureEvent);

    expect(twice.messages).toHaveLength(2);
    expect(twice.messages[1].id).toBe("assistant-request-failed-text-input-1");
  });

  it("打断事件会清空正在显示的助手草稿", () => {
    const state = dispatch(
      {
        ...createInitialChatState(settingsFixture),
        draftAssistantText: "还没说完"
      },
      {
        type: "barge_in.detected",
        payload: {}
      }
    );

    expect(state.draftAssistantText).toBe("");
  });

  it("协议失败空 Final 会清理已经显示的流式草稿", () => {
    const streamingState = dispatch(createInitialChatState(settingsFixture), {
      type: "assistant.text_chunk",
      payload: {
        request_id: "request-a",
        generation_id: "generation-a",
        text: "尚未完成的回复"
      }
    });
    const failedState = dispatch(streamingState, {
      type: "assistant.text_final",
      payload: {
        request_id: "request-a",
        generation_id: "generation-a",
        text: "",
        notice_delivery_failed: true,
        notice_failure_reason: "provider_protocol_leak"
      }
    });

    expect(streamingState.draftAssistantText).toBe("尚未完成的回复");
    expect(failedState.draftAssistantText).toBe("");
    expect(failedState.messages).toEqual([]);
  });

  it("普通设置不再决定后续伴侣消息名称", () => {
    const stateWithAnotherModel = createInitialChatState({
      ...settingsFixture,
      selected_live2d_model_id: "Natori"
    });
    const finalState = dispatch(stateWithAnotherModel, {
      type: "assistant.text_final",
      payload: {
        text: "我还是角色卡里的名字。"
      }
    });

    expect(finalState.companionName).toBe("当前角色");
    expect(finalState.messages.at(-1)).toMatchObject({
      role: "assistant",
      senderName: "当前角色"
    });
  });

  it("当前角色名称变化会同步右栏状态和已有助手消息", () => {
    const stateWithMessage = dispatch(createInitialChatState(settingsFixture), {
      type: "assistant.text_final",
      payload: { text: "这条消息属于当前角色。" }
    });
    const nextState = reduceChatState(stateWithMessage, {
      type: "active_character_name_changed",
      characterName: "  星澜  "
    });

    expect(nextState.companionName).toBe("星澜");
    expect(nextState.messages.at(-1)).toMatchObject({
      role: "assistant",
      senderName: "星澜"
    });
  });

  it("task_result.completed 会追加结果卡消息", () => {
    const state = dispatch(createInitialChatState(settingsFixture), {
      type: "task_result.completed",
      payload: {
        activity_id: "taskResult-1",
        interaction_session_id: "session-a",
        goal: "总结这些材料",
        display_content: "这是整理好的正文。",
        source_materials: ["report.pdf"],
        output_paths: ["G:\\NearMate Workspace\\report.md"],
        created_at: "2026-07-09T10:00:00+08:00"
      }
    });

    expect(state.messages).toMatchObject([
      {
        role: "assistant",
        kind: "task_result",
        senderName: "当前角色",
        taskResult: {
          status: "completed",
          goal: "总结这些材料",
          contentText: "这是整理好的正文。",
          sourceMaterials: ["report.pdf"],
          outputPaths: ["G:\\NearMate Workspace\\report.md"]
        }
      }
    ]);
  });

  it("task_result.failed 会追加失败结果卡", () => {
    const state = dispatch(createInitialChatState(settingsFixture), {
      type: "task_result.failed",
      payload: {
        activity_id: "taskResult-1",
        interaction_session_id: "session-a",
        goal: "总结这些材料",
        failure: {
          stage_code: "loading_materials",
          stage_label: "读取材料",
          code: "material_unavailable",
          summary: "任务所需的材料缺失、损坏或无法读取。",
          technical_detail: "materials=project",
          suggested_action: "请确认材料仍然可以访问后重新提交任务。"
        },
        source_materials: ["project"],
        created_at: "2026-07-09T10:00:00+08:00"
      }
    });

    expect(state.messages.at(-1)).toMatchObject({
      kind: "task_result",
      taskResult: {
        status: "failed",
        goal: "总结这些材料",
        contentText: "任务所需的材料缺失、损坏或无法读取。",
        sourceMaterials: ["project"]
      }
    });
  });

  it("task_result.cancelled 会按同一 task id 原位更新为已取消结果卡", () => {
    const started = dispatch(createInitialChatState(settingsFixture), {
      type: "task_result.started",
      payload: {
        activity_id: "123e4567-e89b-42d3-a456-426614174000",
        interaction_session_id: "session-a",
        goal: "总结这些材料",
        created_at: "2026-07-18T10:00:00+08:00"
      }
    });
    const cancelled = dispatch(started, {
      type: "task_result.cancelled",
      payload: {
        activity_id: "123e4567-e89b-42d3-a456-426614174000",
        interaction_session_id: "session-a",
        goal: "总结这些材料",
        message: "已停止生成。",
        reason: "user_cancelled",
        source_materials: ["report.pdf"],
        created_at: "2026-07-18T10:00:02+08:00"
      }
    });

    expect(cancelled.messages).toHaveLength(1);
    expect(cancelled.messages[0]).toMatchObject({
      id: "taskResult-123e4567-e89b-42d3-a456-426614174000",
      taskResult: {
        status: "cancelled",
        goal: "总结这些材料",
        contentText: "已停止生成。"
      }
    });
  });

  it("同一 taskResult 终态重复到达时原位更新结果卡", () => {
    const initial = createInitialChatState(settingsFixture);
    const first = dispatch(initial, {
      type: "task_result.completed",
      payload: {
        activity_id: "taskResult-1",
        interaction_session_id: "session-a",
        goal: "总结这些材料",
        display_content: "初版正文",
        source_materials: [],
        created_at: 1_788_800_000_000
      }
    });
    const duplicate = dispatch(first, {
      type: "task_result.completed",
      payload: {
        activity_id: "taskResult-1",
        interaction_session_id: "session-a",
        goal: "总结这些材料",
        display_content: "最终正文",
        source_materials: ["report.pdf"],
        created_at: 1_788_800_000_123
      }
    });

    expect(duplicate.messages).toHaveLength(1);
    expect(duplicate.messages[0]).toMatchObject({
      id: "taskResult-taskResult-1",
      createdAt: 1_788_800_000_123,
      taskResult: {
        goal: "总结这些材料",
        contentText: "最终正文",
        sourceMaterials: ["report.pdf"]
      }
    });
  });

  it("后台权限卡按 request id 原位收口", () => {
    const requested = dispatch(createInitialChatState(settingsFixture), {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "request-permission-a",
        activity_id: "activity-a",
        interaction_session_id: "session-a",
        kind: "permission",
        tool_name: "Bash",
        input_data: { command: "ffmpeg -i input.wav output.mp3" },
        questions: [],
        action_summary: "运行完成当前任务所需的命令",
        grant_scope: "activity"
      }
    });
    const resolved = dispatch(requested, {
      type: "background_agent.interaction_resolved",
      payload: {
        request_id: "request-permission-a",
        activity_id: "activity-a",
        outcome: "allowed"
      }
    });

    expect(resolved.messages).toHaveLength(1);
    expect(resolved.messages[0]).toMatchObject({
      id: "agent-interaction-request-permission-a",
      kind: "agent_interaction",
      agentInteraction: {
        requestId: "request-permission-a",
        activityId: "activity-a",
        status: "resolved",
        outcome: "allowed"
      }
    });
  });

  it("后台问题卡把 SDK 问题结构收敛成稳定表单模型", () => {
    const state = dispatch(createInitialChatState(settingsFixture), {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "request-question-a",
        activity_id: "activity-a",
        interaction_session_id: "session-a",
        kind: "question",
        tool_name: "AskUserQuestion",
        input_data: {},
        questions: [{
          header: "格式",
          question: "输出格式？",
          options: [{ label: "MP3", description: "兼容性更好" }],
          multiSelect: false
        }]
      }
    });

    expect(state.messages[0].agentInteraction?.questions).toEqual([{
      header: "格式",
      question: "输出格式？",
      options: [{ label: "MP3", description: "兼容性更好" }],
      multiSelect: false
    }]);
  });

  it("迟到的 submitted 事件不能把已收口卡片退回处理中", () => {
    const requested = dispatch(createInitialChatState(settingsFixture), {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "request-permission-race",
        activity_id: "activity-a",
        interaction_session_id: "session-a",
        kind: "permission",
        tool_name: "Bash",
        input_data: { command: "echo done" },
        questions: [],
        action_summary: "运行完成当前任务所需的命令",
        grant_scope: "activity"
      }
    });
    const resolved = dispatch(requested, {
      type: "background_agent.interaction_resolved",
      payload: {
        request_id: "request-permission-race",
        activity_id: "activity-a",
        outcome: "allowed"
      }
    });

    const afterLateSubmitted = dispatch(resolved, {
      type: "background_agent.interaction_submitted",
      payload: {
        request_id: "request-permission-race",
        activity_id: "activity-a"
      }
    });

    expect(afterLateSubmitted.messages[0].agentInteraction).toMatchObject({
      status: "resolved",
      outcome: "allowed"
    });
  });

  it("后台待答快照删除失效 pending 卡并保留已收口历史", () => {
    const initial = createInitialChatState(settingsFixture);
    const pending = dispatch(initial, {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "request-pending-a",
        activity_id: "activity-a",
        interaction_session_id: "session-a",
        kind: "permission",
        tool_name: "Bash",
        input_data: { command: "echo stale" },
        questions: [],
        action_summary: "运行完成当前任务所需的命令",
        grant_scope: "activity"
      }
    });
    const processing = dispatch(pending, {
      type: "background_agent.interaction_submitted",
      payload: {
        request_id: "request-pending-a",
        activity_id: "activity-a"
      }
    });
    const secondPending = dispatch(processing, {
      type: "background_agent.interaction_requested",
      payload: {
        request_id: "request-resolved-b",
        activity_id: "activity-a",
        interaction_session_id: "session-a",
        kind: "permission",
        tool_name: "Bash",
        input_data: { command: "echo done" },
        questions: [],
        action_summary: "运行完成当前任务所需的命令",
        grant_scope: "activity"
      }
    });
    const withHistory = dispatch(secondPending, {
      type: "background_agent.interaction_resolved",
      payload: {
        request_id: "request-resolved-b",
        activity_id: "activity-a",
        outcome: "allowed"
      }
    });

    const synchronized = dispatch(withHistory, {
      type: "background_agent.interactions_snapshot",
      payload: {
        items: [{
          request_id: "request-current-c",
          activity_id: "activity-a",
          interaction_session_id: "session-a",
          status: "processing",
          kind: "question",
          tool_name: "AskUserQuestion",
          input_data: {},
          questions: [{
            header: "格式",
            question: "输出格式？",
            options: [],
            multiSelect: true
          }]
        }]
      }
    });

    expect(synchronized.messages.map(message => message.id)).toEqual([
      "agent-interaction-request-resolved-b",
      "agent-interaction-request-current-c"
    ]);
    expect(synchronized.messages[0].agentInteraction?.status).toBe("resolved");
    expect(synchronized.messages[1].agentInteraction?.status).toBe("processing");
    expect(synchronized.messages[1].agentInteraction?.questions[0].multiSelect).toBe(true);
  });

  it("乐观文本在旧快照到达时保留，并由较新快照原位确认", () => {
    const submissionId = "00000000-0000-4000-8000-000000000101";
    const initial = createInitialChatState(settingsFixture);
    const withInitialSnapshot = reduceChatState(initial, {
      type: "turn_submissions_snapshot_received",
      snapshot: { revision: 3, items: [] }
    });
    const optimistic = reduceChatState(withInitialSnapshot, {
      type: "turn_submission_optimistically_added",
      submissionId,
      messageText: "先发这条"
    });
    const afterStaleSnapshot = reduceChatState(optimistic, {
      type: "turn_submissions_snapshot_received",
      snapshot: { revision: 3, items: [] }
    });
    const confirmed = reduceChatState(afterStaleSnapshot, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 4,
        items: [{
          submissionId,
          messageText: "先发这条",
          state: "queued",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });

    expect(afterStaleSnapshot.messages).toHaveLength(1);
    expect(confirmed.messages[0]).toMatchObject({
      id: `turn-submission-${submissionId}`,
      submissionId,
      submissionState: "queued",
      submissionAcknowledged: true
    });
  });

  it("Final ASR 暂停待发文本时，快照原位切换为 paused 而不改变正文顺序", () => {
    const firstSubmissionId = "00000000-0000-4000-8000-000000000111";
    const secondSubmissionId = "00000000-0000-4000-8000-000000000112";
    const queued = reduceChatState(createInitialChatState(settingsFixture), {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 5,
        items: [
          {
            submissionId: firstSubmissionId,
            messageText: "第一条文本",
            state: "queued",
            createdOrder: 1,
            errorMessage: null
          },
          {
            submissionId: secondSubmissionId,
            messageText: "第二条文本",
            state: "preparing",
            createdOrder: 2,
            errorMessage: null
          }
        ]
      }
    });
    const paused = reduceChatState(queued, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 6,
        items: [
          {
            submissionId: firstSubmissionId,
            messageText: "第一条文本",
            state: "paused",
            createdOrder: 1,
            errorMessage: null
          },
          {
            submissionId: secondSubmissionId,
            messageText: "第二条文本",
            state: "paused",
            createdOrder: 2,
            errorMessage: null
          }
        ]
      }
    });

    expect(paused.messages.map(message => [message.text, message.submissionState])).toEqual([
      ["第一条文本", "paused"],
      ["第二条文本", "paused"]
    ]);
  });

  it("正式用户事件按 submission_id 原位收口，并抵御迟到快照和重复事件", () => {
    const submissionId = "00000000-0000-4000-8000-000000000121";
    const queued = reduceChatState(createInitialChatState(settingsFixture), {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 7,
        items: [{
          submissionId,
          messageText: "排队正文",
          state: "queued",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });
    const finalizedEvent: BackendEvent = {
      type: "user.final_transcript",
      payload: {
        session_id: "desktop-text",
        turn_id: null,
        request_id: `text-input-${submissionId}`,
        submission_id: submissionId,
        history_disposition: "ordinary_turn",
        text: "排队正文"
      }
    };
    const finalized = dispatch(queued, finalizedEvent);
    const afterLateSnapshot = reduceChatState(finalized, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 8,
        items: [{
          submissionId,
          messageText: "不应复活的旧正文",
          state: "paused",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });
    const afterDuplicateFinal = dispatch(afterLateSnapshot, finalizedEvent);

    expect(afterDuplicateFinal.messages).toHaveLength(1);
    expect(afterDuplicateFinal.messages[0]).toMatchObject({
      id: `turn-submission-${submissionId}`,
      requestId: `text-input-${submissionId}`,
      text: "排队正文",
      submissionId
    });
    expect(afterDuplicateFinal.messages[0].submissionState).toBeUndefined();
  });

  it("reservation 失败只撤销尚未被 Backend 确认的乐观气泡", () => {
    const localSubmissionId = "00000000-0000-4000-8000-000000000131";
    const confirmedSubmissionId = "00000000-0000-4000-8000-000000000132";
    let state = createInitialChatState(settingsFixture);
    state = reduceChatState(state, {
      type: "turn_submission_optimistically_added",
      submissionId: localSubmissionId,
      messageText: "本地失败"
    });
    state = reduceChatState(state, {
      type: "turn_submissions_snapshot_received",
      snapshot: {
        revision: 1,
        items: [{
          submissionId: confirmedSubmissionId,
          messageText: "已被确认",
          state: "preparing",
          createdOrder: 1,
          errorMessage: null
        }]
      }
    });
    state = reduceChatState(state, {
      type: "turn_submission_reservation_rejected",
      submissionId: localSubmissionId
    });
    state = reduceChatState(state, {
      type: "turn_submission_reservation_rejected",
      submissionId: confirmedSubmissionId
    });

    expect(state.messages.map(message => message.submissionId)).toEqual([
      confirmedSubmissionId
    ]);
  });
});

function dispatch(state: ChatState, event: unknown): ChatState {
  return reduceChatState(state, {
    type: "backend_event_received",
    event: event as BackendEvent
  });
}
