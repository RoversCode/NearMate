import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ActivityDetail,
  ActivitySnapshot
} from "../../api/contracts/activities";
import {
  ActivityApiError,
  cancelActivity,
  fetchActivities,
  fetchActivityDetail
} from "../../api/services/activities";
import { connectControlSocket, type ControlSocketConnection } from "../../lib/controlSocket";
import { isStandardUuid } from "../../lib/uuid";

type ActivityDetailState =
  | { status: "idle"; activity: null; errorMessage: null }
  | { status: "loading"; activity: null; errorMessage: null }
  | { status: "ready"; activity: ActivityDetail; errorMessage: null }
  | { status: "stale"; activity: null; errorMessage: null }
  | { status: "failed"; activity: null; errorMessage: string };

const INITIAL_DETAIL_STATE: ActivityDetailState = {
  status: "idle",
  activity: null,
  errorMessage: null
};
const RESTART_NOTICE = "应用服务已重新启动，未完成的任务已结束。";

/**
 * 管理任务中心的 feature-local HTTP、WebSocket 失效和取消状态。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   selectedActivityId: URL 当前选中的 Activity UUID；列表态为空。
 *
 * Returns:
 *   列表、详情、连接与重启提示、错误、取消请求集合和刷新命令。
 */
export function useActivitiesRuntime(
  baseUrl: string,
  selectedActivityId: string | null
) {
  const [snapshot, setSnapshot] = useState<ActivitySnapshot | null>(null);
  const [isLoadingList, setIsLoadingList] = useState(true);
  const [listErrorMessage, setListErrorMessage] = useState<string | null>(null);
  const [connectionMessage, setConnectionMessage] = useState<string | null>(null);
  const [restartMessage, setRestartMessage] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<ActivityDetailState>(INITIAL_DETAIL_STATE);
  const [cancellingIds, setCancellingIds] = useState<Set<string>>(() => new Set());
  const [cancelErrorMessage, setCancelErrorMessage] = useState<string | null>(null);
  const listRequestRef = useRef<{ sequence: number; controller: AbortController } | null>(null);
  const detailRequestRef = useRef<{ sequence: number; controller: AbortController } | null>(null);
  const selectedActivityIdRef = useRef(selectedActivityId);
  const cancellingIdsRef = useRef(cancellingIds);
  const snapshotRef = useRef(snapshot);
  const detailStateRef = useRef(detailState);

  /** 同步发布权威列表快照与供异步回调读取的版本引用。 */
  const commitSnapshot = useCallback((nextSnapshot: ActivitySnapshot | null): void => {
    snapshotRef.current = nextSnapshot;
    setSnapshot(nextSnapshot);
  }, []);

  /** 同步发布详情视图状态与供异步回调读取的最新状态引用。 */
  const commitDetailState = useCallback((nextState: ActivityDetailState): void => {
    detailStateRef.current = nextState;
    setDetailState(nextState);
  }, []);

  /** 清空只属于旧 Backend 进程的任务交互状态。 */
  const resetAfterRuntimeRestart = useCallback((): void => {
    setRestartMessage(RESTART_NOTICE);
    commitSnapshot(null);
    const clearedCancelling = new Set<string>();
    cancellingIdsRef.current = clearedCancelling;
    setCancellingIds(clearedCancelling);
    setCancelErrorMessage(null);
    commitDetailState(INITIAL_DETAIL_STATE);
  }, [commitDetailState, commitSnapshot]);

  useEffect(() => {
    selectedActivityIdRef.current = selectedActivityId;
  }, [selectedActivityId]);

  useEffect(() => {
    cancellingIdsRef.current = cancellingIds;
  }, [cancellingIds]);

  const loadDetail = useCallback(async (
    activityId: string,
    options?: { background?: boolean }
  ): Promise<void> => {
    const background = options?.background ?? false;
    detailRequestRef.current?.controller.abort();
    const sequence = (detailRequestRef.current?.sequence ?? 0) + 1;
    const controller = new AbortController();
    detailRequestRef.current = { sequence, controller };
    // 同一详情的后台刷新原地更新，不回退到加载占位，避免页面闪烁。
    const previous = detailStateRef.current;
    const keepCurrent =
      background &&
      previous.status === "ready" &&
      previous.activity.activity_id === activityId;
    if (!keepCurrent) {
      commitDetailState({ status: "loading", activity: null, errorMessage: null });
    }

    try {
      const activity = await fetchActivityDetail(baseUrl, activityId, controller.signal);
      if (detailRequestRef.current?.sequence !== sequence) {
        return;
      }
      commitDetailState({ status: "ready", activity, errorMessage: null });
      setConnectionMessage(null);
    } catch (error) {
      if (controller.signal.aborted || detailRequestRef.current?.sequence !== sequence) {
        return;
      }
      // 只有记录真正不存在时才进入 stale 页面。
      if (error instanceof ActivityApiError && error.status === 404) {
        commitDetailState({ status: "stale", activity: null, errorMessage: null });
        return;
      }
      if (keepCurrent) {
        setConnectionMessage("暂时无法刷新任务详情，展示的是最近一次数据。");
        return;
      }
      commitDetailState({
        status: "failed",
        activity: null,
        errorMessage: error instanceof Error ? error.message : "读取任务详情失败"
      });
    }
  }, [baseUrl, commitDetailState]);

  const loadList = useCallback(async (options?: { background?: boolean }): Promise<void> => {
    const background = options?.background ?? false;
    listRequestRef.current?.controller.abort();
    const sequence = (listRequestRef.current?.sequence ?? 0) + 1;
    const controller = new AbortController();
    listRequestRef.current = { sequence, controller };
    // 只有首次或 runtime 重启后的空数据加载才显示加载占位；后台刷新保留旧列表。
    if (!background) {
      setIsLoadingList(true);
    }

    try {
      const nextSnapshot = await fetchActivities(baseUrl, controller.signal);
      if (listRequestRef.current?.sequence !== sequence) {
        return;
      }
      const currentSnapshot = snapshotRef.current;
      const runtimeChanged =
        currentSnapshot !== null &&
        currentSnapshot.runtime_id !== nextSnapshot.runtime_id;
      // WebSocket 不可用时，HTTP 仍须独立识别重启并清空旧运行期交互状态。
      if (runtimeChanged) {
        resetAfterRuntimeRestart();
      }
      commitSnapshot(nextSnapshot);
      setListErrorMessage(null);
      setConnectionMessage(null);
      const activityId = selectedActivityIdRef.current;
      if (runtimeChanged && activityId !== null) {
        void loadDetail(activityId);
      }
    } catch (error) {
      if (controller.signal.aborted || listRequestRef.current?.sequence !== sequence) {
        return;
      }
      // 后台刷新失败保留旧数据，只显示轻量连接提示。
      if (snapshotRef.current === null) {
        setListErrorMessage(error instanceof Error ? error.message : "读取任务列表失败");
      } else {
        setConnectionMessage("暂时无法刷新任务状态，展示的是最近一次数据。");
      }
    } finally {
      // 后台请求可能接替被中止的首次加载；最新请求结束时都必须复位加载占位。
      if (!controller.signal.aborted && listRequestRef.current?.sequence === sequence) {
        setIsLoadingList(false);
      }
    }
  }, [baseUrl, commitSnapshot, loadDetail, resetAfterRuntimeRestart]);

  useEffect(() => {
    void loadList();
    return () => listRequestRef.current?.controller.abort();
  }, [loadList]);

  useEffect(() => {
    if (selectedActivityId === null) {
      detailRequestRef.current?.controller.abort();
      commitDetailState(INITIAL_DETAIL_STATE);
      return;
    }
    void loadDetail(selectedActivityId);
    return () => detailRequestRef.current?.controller.abort();
  }, [commitDetailState, loadDetail, selectedActivityId]);

  useEffect(() => {
    let disposed = false;
    let connection: ControlSocketConnection | null = null;

    const connect = (): void => {
      connection = connectControlSocket(baseUrl, {
        onMessage(message) {
          if (message.type !== "activities.snapshot_changed") {
            return;
          }
          const envelope = readSnapshotEnvelope(message.payload);
          if (envelope === null) {
            return;
          }
          // 合法快照证明控制连接恢复，清除断线提示。
          setConnectionMessage(null);
          const current = snapshotRef.current;
          if (current !== null && envelope.runtimeId !== current.runtime_id) {
            // runtime 改变：清空旧任务、取消中占位和旧详情，再重新获取权威快照。
            resetAfterRuntimeRestart();
            void loadList();
            const activityId = selectedActivityIdRef.current;
            if (activityId !== null) {
              void loadDetail(activityId);
            }
            return;
          }
          // 同一 runtime 且通知 revision 不高于当前快照时忽略。
          if (current !== null && envelope.revision <= current.revision) {
            return;
          }
          // revision 只在单次后端进程内单调递增；更高 revision 使 HTTP 权威数据失效。
          void loadList({ background: true });
          const activityId = selectedActivityIdRef.current;
          // 过期详情页不再跟随无关 Activity 变化反复 loading。
          if (activityId !== null && detailStateRef.current.status !== "stale") {
            void loadDetail(activityId, { background: true });
          }
        },
        onClose() {
          if (disposed) {
            return;
          }
          // 普通断线不改变任务状态；连接恢复后由初始快照重新校准。
          setConnectionMessage("任务状态连接已断开，正在重连。");
        }
      });
    };

    connect();
    return () => {
      disposed = true;
      connection?.close();
    };
  }, [baseUrl, loadDetail, loadList, resetAfterRuntimeRestart]);

  const requestCancel = useCallback(async (activityId: string): Promise<void> => {
    if (cancellingIdsRef.current.has(activityId)) {
      return;
    }
    setCancelErrorMessage(null);
    const nextCancellingIds = new Set(cancellingIdsRef.current).add(activityId);
    cancellingIdsRef.current = nextCancellingIds;
    setCancellingIds(nextCancellingIds);
    try {
      await cancelActivity(baseUrl, activityId);
      await Promise.all([
        loadList({ background: true }),
        selectedActivityIdRef.current === activityId
          ? loadDetail(activityId, { background: true })
          : Promise.resolve()
      ]);
    } catch (error) {
      setCancelErrorMessage(error instanceof Error ? error.message : "取消任务失败");
    } finally {
      setCancellingIds(current => {
        const next = new Set(current);
        next.delete(activityId);
        cancellingIdsRef.current = next;
        return next;
      });
    }
  }, [baseUrl, loadDetail, loadList]);

  return {
    snapshot,
    isLoadingList,
    listErrorMessage,
    connectionMessage,
    restartMessage,
    detailState,
    cancellingIds,
    cancelErrorMessage,
    requestCancel,
    reloadList: loadList,
    reloadDetail: selectedActivityId === null
      ? null
      : () => loadDetail(selectedActivityId)
  };
}

/**
 * 从 WebSocket 失效信号中读取快照版本信封。
 *
 * Args:
 *   payload: 未受信任的事件载荷。
 *
 * Returns:
 *   合法 runtime_id 与 revision；载荷非法时返回空。
 */
function readSnapshotEnvelope(
  payload: unknown
): { runtimeId: string; revision: number } | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
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
