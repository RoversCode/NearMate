import { describe, expect, it } from "vitest";
import type { ActivityCompact, BackendEvent } from "../types/backendEvents";
import type { InteractionStateSnapshot } from "../types/interactionBackendEvents";
import {
  createInitialCompanionStatusState, projectCompanionStatus,
  reduceCompanionStatusState, resetCompanionStatusConnection,
  type CompanionStatusState
} from "./companionStatus";

const snapshot: InteractionStateSnapshot = {
  input_runtime_id: "input-a", input_runtime_epoch: 1, revision: 1,
  followup_state: "followup_cool", turn_state: "idle", session_id: "session-a"
};
const task: ActivityCompact = {
  activity_id: "task-a", kind: "background_agent", type_label: "后台工作", status: "running",
  goal_summary: "整理资料", failure_summary: null, stage_code: "processing", stage_label: "处理中",
  started_at: "2026-10-10T12:00:00Z", updated_at: "2026-10-10T12:00:00Z", finished_at: null, can_cancel: true
};

function receive(state: CompanionStatusState, changes: Partial<InteractionStateSnapshot> = {}) {
  return reduceCompanionStatusState(state, { type: "interaction.state_changed", payload: { ...snapshot, ...changes } });
}
function connected() {
  return resetCompanionStatusConnection(createInitialCompanionStatusState(), "connected");
}

describe("当前角色统一状态", () => {
  it("收到权威模式前显示同步中，不能把链路连接当作已唤醒", () => {
    expect(projectCompanionStatus(connected(), []).state).toBe("syncing");
    expect(projectCompanionStatus(receive(connected()), []).label).toBe("等待唤醒");
    expect(projectCompanionStatus(receive(connected(), { followup_state: "followup_hot" }), []).label).toBe("无需再唤醒");
  });

  it.each([ ["listening", "listening"], ["processing", "thinking"], ["speaking", "responding"] ] as const)(
    "重连首帧恢复 %s，无需等待新正文或麦克风声音", (turn_state, expected) => {
      expect(projectCompanionStatus(receive(connected(), { turn_state }), []).state).toBe(expected);
    }
  );

  it("任务运行时保留独立唤醒要求，用户开口后聆听状态优先", () => {
    const working = receive(connected(), { followup_state: "followup_hot" });
    expect(projectCompanionStatus(working, [task])).toMatchObject({ state: "working", wakeLabel: "无需再唤醒" });
    const listening = receive(working, { revision: 2, turn_state: "listening", followup_state: "followup_hot" });
    expect(projectCompanionStatus(listening, [task]).state).toBe("listening");
  });

  it("根据真实待答卡区分授权与资料补充", () => {
    const state = reduceCompanionStatusState(receive(connected()), {
      type: "background_agent.interactions_snapshot",
      payload: { items: [{ request_id: "request-a", activity_id: "task-a", interaction_session_id: "session-a",
        tool_name: "read_file", input_data: {}, questions: [], kind: "permission", status: "pending",
        action_summary: "读取文件", grant_scope: "activity" }] }
    });
    expect(projectCompanionStatus(state, [{ ...task, status: "waiting_user" }]).label).toBe("等你授权");
    const resolved = reduceCompanionStatusState(state, { type: "background_agent.interaction_resolved",
      payload: { request_id: "request-a", activity_id: "task-a", outcome: "allowed" } });
    expect(projectCompanionStatus(resolved, [task]).state).toBe("working");
  });

  it("拒绝旧版本、旧运行时和同世代的冲突身份", () => {
    const state = receive(connected(), { revision: 5, followup_state: "followup_hot" });
    expect(receive(state, { revision: 4 })).toBe(state);
    expect(receive(state, { revision: 9, input_runtime_epoch: 0 })).toBe(state);
    expect(receive(state, { revision: 9, input_runtime_id: "input-conflict" })).toBe(state);
    expect(projectCompanionStatus(state, []).state).toBe("available");
  });

  it("同版本手动回执只强调一次，重复回执不重放", () => {
    const state = receive(connected());
    const toggled = receive(state, { source: "manual_toggle" });
    expect(toggled.manualToggleRevision).toBe(1);
    expect(receive(toggled, { source: "manual_toggle" })).toBe(toggled);
  });

  it("停止语音的新快照先到时，迟到的手动回执仍确认切换但不倒退状态", () => {
    const current = receive(connected(), { revision: 5, turn_state: "idle", followup_state: "followup_hot" });
    const toggled = receive(current, {
      revision: 4, turn_state: "speaking", followup_state: "followup_hot", source: "manual_toggle"
    });
    expect(toggled.interaction).toBe(current.interaction);
    expect(toggled.manualToggleRevision).toBe(4);
    expect(projectCompanionStatus(toggled, []).state).toBe("available");
    expect(receive(toggled, { revision: 4, source: "manual_toggle" })).toBe(toggled);
    expect(receive(toggled, { revision: 3, source: "manual_toggle" })).toBe(toggled);
    expect(receive(toggled, { revision: 8, input_runtime_id: "old-runtime", source: "manual_toggle" })).toBe(toggled);
    expect(receive(toggled, { revision: 8, input_runtime_epoch: 0, source: "manual_toggle" })).toBe(toggled);
  });

  it("权威快照到达后迟到音频和正文不会改变状态", () => {
    const state = receive(connected(), { turn_state: "processing" });
    const oldOutput = { type: "assistant.audio_finished", payload: { request_id: "old", session_id: "session-a", generation_id: "old" } } as BackendEvent;
    expect(reduceCompanionStatusState(state, oldOutput)).toBe(state);
    expect(projectCompanionStatus(state, []).state).toBe("thinking");
  });

  it("新输入运行时接管时撤下旧可聊天状态，不能由迟到旧快照恢复", () => {
    const state = receive(connected(), { followup_state: "followup_hot" });
    const next = reduceCompanionStatusState(state, { type: "input.runtime_changed",
      payload: { input_runtime_id: "input-b", input_runtime_epoch: 2 } });
    expect(projectCompanionStatus(next, []).state).toBe("syncing");
    expect(receive(next, { revision: 9 })).toBe(next);
    expect(projectCompanionStatus(receive(next, { input_runtime_id: "input-b", input_runtime_epoch: 2, revision: 0 }), []).state).toBe("waiting_wake");
  });

  it("断线和输出不可用不会显示可聊天状态，重新连接清除旧版本门禁", () => {
    const state = receive(connected(), { revision: 5, followup_state: "followup_hot" });
    expect(projectCompanionStatus(resetCompanionStatusConnection(state, "disconnected"), []).state).toBe("error");
    const unhealthy = reduceCompanionStatusState(state, { type: "assistant.output_status_changed",
      payload: { available: false, code: "output_unavailable", message: "当前输出不可用" } });
    expect(projectCompanionStatus(unhealthy, []).state).toBe("error");
    const reconnecting = resetCompanionStatusConnection(state, "connecting");
    expect(reconnecting.interaction).toBeNull();
    expect(receive(resetCompanionStatusConnection(reconnecting, "connected"), { revision: 0 }).interaction?.revision).toBe(0);
  });

  it("旧 Backend 的健康恢复能清掉请求错误，并保留跟进模式", () => {
    const hot = reduceCompanionStatusState(connected(), { type: "followup.state_changed",
      payload: { followup_state: "followup_hot" } });
    const failed = reduceCompanionStatusState(hot, { type: "assistant.request_failed",
      payload: { request_id: "old-request", code: "output_unavailable", message: "旧请求失败" } });
    expect(projectCompanionStatus(failed, []).state).toBe("error");
    const recovered = reduceCompanionStatusState(failed, { type: "assistant.output_status_changed",
      payload: { available: true, code: null, message: null } });
    expect(projectCompanionStatus(recovered, []).state).toBe("available");
  });
});
