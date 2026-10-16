import { useCallback, useEffect, useRef, useState } from "react";
import type { TimedReminderSnapshot } from "../../api/contracts/timedReminders";
import {
  cancelTimedReminder,
  dismissTimedReminders,
  fetchTimedReminders,
  updateTimedReminder,
  type TimedReminderUpdate
} from "../../api/services/timedReminders";
import { connectControlSocket, type ControlSocketConnection } from "../../lib/controlSocket";
import { isStandardUuid } from "../../lib/uuid";

const RESTART_NOTICE = "Backend 已重启，上一运行期的提醒已清空。";

/**
 * 管理定时提醒页的 HTTP 权威快照、WebSocket 失效和写请求状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   当前快照、加载/连接提示、写请求集合以及修改和取消命令。
 */
export function useTimedRemindersRuntime(baseUrl: string) {
  const [snapshot, setSnapshot] = useState<TimedReminderSnapshot | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadErrorMessage, setLoadErrorMessage] = useState<string | null>(null);
  const [connectionMessage, setConnectionMessage] = useState<string | null>(null);
  const [restartMessage, setRestartMessage] = useState<string | null>(null);
  const [mutationErrorMessage, setMutationErrorMessage] = useState<string | null>(null);
  const [requestingIds, setRequestingIds] = useState<Set<string>>(() => new Set());
  const snapshotRef = useRef(snapshot);
  const requestingIdsRef = useRef(requestingIds);
  const listRequestRef = useRef<{ sequence: number; controller: AbortController } | null>(null);

  /** 同步提交快照和异步回调使用的最新引用。 */
  const commitSnapshot = useCallback((nextSnapshot: TimedReminderSnapshot | null): void => {
    snapshotRef.current = nextSnapshot;
    setSnapshot(nextSnapshot);
  }, []);

  /** 清空只属于旧 Backend 运行期的提醒与请求占位。 */
  const resetAfterRuntimeRestart = useCallback((): void => {
    setRestartMessage(RESTART_NOTICE);
    commitSnapshot(null);
    const clearedIds = new Set<string>();
    requestingIdsRef.current = clearedIds;
    setRequestingIds(clearedIds);
    setMutationErrorMessage(null);
  }, [commitSnapshot]);

  /**
   * 从 HTTP 读取完整权威快照，并用请求序号淘汰迟到响应。
   *
   * Args:
   *   options.background: 后台刷新时保留当前列表，避免界面闪烁。
   *
   * Returns:
   *   最新请求结算后完成。
   */
  const loadSnapshot = useCallback(async (
    options?: { background?: boolean }
  ): Promise<void> => {
    const background = options?.background ?? false;
    listRequestRef.current?.controller.abort();
    const sequence = (listRequestRef.current?.sequence ?? 0) + 1;
    const controller = new AbortController();
    listRequestRef.current = { sequence, controller };
    if (!background) {
      setIsLoading(true);
    }
    try {
      const nextSnapshot = await fetchTimedReminders(baseUrl, controller.signal);
      if (listRequestRef.current?.sequence !== sequence) {
        return;
      }
      const current = snapshotRef.current;
      if (current !== null && current.runtime_id !== nextSnapshot.runtime_id) {
        resetAfterRuntimeRestart();
      }
      commitSnapshot(nextSnapshot);
      setLoadErrorMessage(null);
      setConnectionMessage(null);
    } catch (error) {
      if (controller.signal.aborted || listRequestRef.current?.sequence !== sequence) {
        return;
      }
      if (snapshotRef.current === null) {
        setLoadErrorMessage(error instanceof Error ? error.message : "读取定时提醒失败");
      } else {
        setConnectionMessage("暂时无法刷新提醒状态，展示的是最近一次数据。");
      }
    } finally {
      if (!controller.signal.aborted && listRequestRef.current?.sequence === sequence) {
        setIsLoading(false);
      }
    }
  }, [baseUrl, commitSnapshot, resetAfterRuntimeRestart]);

  useEffect(() => {
    void loadSnapshot();
    return () => listRequestRef.current?.controller.abort();
  }, [loadSnapshot]);

  useEffect(() => {
    let disposed = false;
    let connection: ControlSocketConnection | null = null;
    connection = connectControlSocket(baseUrl, {
      onMessage(message) {
        if (message.type !== "reminders.snapshot_changed") {
          return;
        }
        const envelope = readSnapshotEnvelope(message.payload);
        if (envelope === null) {
          return;
        }
        setConnectionMessage(null);
        const current = snapshotRef.current;
        if (current !== null && envelope.runtimeId !== current.runtime_id) {
          resetAfterRuntimeRestart();
          void loadSnapshot();
          return;
        }
        if (current !== null && envelope.revision <= current.revision) {
          return;
        }
        void loadSnapshot({ background: true });
      },
      onClose() {
        if (!disposed) {
          setConnectionMessage("提醒状态连接已断开，正在重连。");
        }
      }
    });
    return () => {
      disposed = true;
      connection?.close();
    };
  }, [baseUrl, loadSnapshot, resetAfterRuntimeRestart]);

  /** 提交提醒修改，并在成功后立即重读 HTTP 权威快照。 */
  const requestUpdate = useCallback(async (
    reminderId: string,
    update: TimedReminderUpdate
  ): Promise<boolean> => {
    if (!markRequesting(reminderId, true, requestingIdsRef, setRequestingIds)) {
      return false;
    }
    const requestRuntimeId = snapshotRef.current?.runtime_id ?? null;
    setMutationErrorMessage(null);
    try {
      await updateTimedReminder(baseUrl, reminderId, update);
      await loadSnapshot({ background: true });
      return true;
    } catch (error) {
      if ((snapshotRef.current?.runtime_id ?? null) !== requestRuntimeId) {
        return false;
      }
      setMutationErrorMessage(error instanceof Error ? error.message : "修改提醒失败");
      return false;
    } finally {
      if ((snapshotRef.current?.runtime_id ?? null) === requestRuntimeId) {
        markRequesting(reminderId, false, requestingIdsRef, setRequestingIds);
      }
    }
  }, [baseUrl, loadSnapshot]);

  /** 提交提醒取消，并在成功后立即重读 HTTP 权威快照。 */
  const requestCancel = useCallback(async (reminderId: string): Promise<void> => {
    if (!markRequesting(reminderId, true, requestingIdsRef, setRequestingIds)) {
      return;
    }
    const requestRuntimeId = snapshotRef.current?.runtime_id ?? null;
    setMutationErrorMessage(null);
    try {
      await cancelTimedReminder(baseUrl, reminderId);
      await loadSnapshot({ background: true });
    } catch (error) {
      if ((snapshotRef.current?.runtime_id ?? null) === requestRuntimeId) {
        setMutationErrorMessage(error instanceof Error ? error.message : "取消提醒失败");
      }
    } finally {
      if ((snapshotRef.current?.runtime_id ?? null) === requestRuntimeId) {
        markRequesting(reminderId, false, requestingIdsRef, setRequestingIds);
      }
    }
  }, [baseUrl, loadSnapshot]);

  /** 原子关闭当前响铃批次；成功后等待 HTTP 权威快照完成收口。 */
  const requestDismiss = useCallback(async (
    runtimeId: string,
    reminderIds: readonly string[]
  ): Promise<void> => {
    const requestRuntimeId = snapshotRef.current?.runtime_id ?? null;
    if (requestRuntimeId !== runtimeId) {
      return;
    }
    if (!markBatchRequesting(reminderIds, requestingIdsRef, setRequestingIds)) {
      return;
    }
    setMutationErrorMessage(null);
    try {
      await dismissTimedReminders(baseUrl, runtimeId, reminderIds);
      await loadSnapshot({ background: true });
    } catch (error) {
      if ((snapshotRef.current?.runtime_id ?? null) === requestRuntimeId) {
        setMutationErrorMessage(error instanceof Error ? error.message : "关闭提醒失败");
      }
    } finally {
      if ((snapshotRef.current?.runtime_id ?? null) === requestRuntimeId) {
        clearBatchRequesting(reminderIds, requestingIdsRef, setRequestingIds);
      }
    }
  }, [baseUrl, loadSnapshot]);

  return {
    snapshot,
    isLoading,
    loadErrorMessage,
    connectionMessage,
    restartMessage,
    mutationErrorMessage,
    requestingIds,
    requestUpdate,
    requestCancel,
    requestDismiss,
    reload: loadSnapshot
  };
}

/** 为当前气泡展示的整批编号一次性占位，任何一项在途时都拒绝重复提交。 */
function markBatchRequesting(
  reminderIds: readonly string[],
  requestingIdsRef: React.MutableRefObject<Set<string>>,
  setRequestingIds: React.Dispatch<React.SetStateAction<Set<string>>>
): boolean {
  if (reminderIds.length === 0 || reminderIds.some(id => requestingIdsRef.current.has(id))) {
    return false;
  }
  const next = new Set(requestingIdsRef.current);
  reminderIds.forEach(id => next.add(id));
  requestingIdsRef.current = next;
  setRequestingIds(next);
  return true;
}

/** 释放指定批次占位，不影响同时发生的其它提醒写操作。 */
function clearBatchRequesting(
  reminderIds: readonly string[],
  requestingIdsRef: React.MutableRefObject<Set<string>>,
  setRequestingIds: React.Dispatch<React.SetStateAction<Set<string>>>
): void {
  const next = new Set(requestingIdsRef.current);
  reminderIds.forEach(id => next.delete(id));
  requestingIdsRef.current = next;
  setRequestingIds(next);
}

/** 同步更新请求集合和 ref；重复占位时不再次执行写请求。 */
function markRequesting(
  reminderId: string,
  requesting: boolean,
  requestingIdsRef: React.MutableRefObject<Set<string>>,
  setRequestingIds: React.Dispatch<React.SetStateAction<Set<string>>>
): boolean {
  if (requesting && requestingIdsRef.current.has(reminderId)) {
    return false;
  }
  const next = new Set(requestingIdsRef.current);
  if (requesting) {
    next.add(reminderId);
  } else {
    next.delete(reminderId);
  }
  requestingIdsRef.current = next;
  setRequestingIds(next);
  return true;
}

/** 从 WebSocket 事件里读取最小运行身份和版本信封。 */
function readSnapshotEnvelope(
  payload: unknown
): { runtimeId: string; revision: number } | null {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const candidate = payload as { runtime_id?: unknown; revision?: unknown };
  if (typeof candidate.runtime_id !== "string" || !isStandardUuid(candidate.runtime_id)) {
    return null;
  }
  if (!Number.isInteger(candidate.revision) || (candidate.revision as number) < 0) {
    return null;
  }
  return { runtimeId: candidate.runtime_id, revision: candidate.revision as number };
}
