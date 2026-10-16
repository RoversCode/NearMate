import {
  startTransition,
  useCallback,
  useEffect,
  useRef,
  useState,
  type MutableRefObject
} from "react";
import type { ReminderSettings } from "../types/events";
import type { CompanionPanelBridge } from "./panelBridge";

// IPC 接纳本应立即返回；只等待接纳回执，已接纳的模型加载不受这个时限影响。
const MODEL_SWITCH_ACCEPTANCE_TIMEOUT_MS = 5000;

/**
 * 提供人物切换的请求身份、同步锁与可展示的失败状态。
 *
 * Args:
 *   不适用；这是人物面板使用的状态接口。
 * Returns:
 *   当前模型与请求编号、提交命令，以及成功和失败收口回调。
 */
export type PanelModelSwitchRuntime = {
  pendingModelId: string | null;
  pendingModelIdRef: MutableRefObject<string | null>;
  pendingRequestIdRef: MutableRefObject<string | null>;
  modelSwitchError: string | null;
  clearModelSwitchPending(): void;
  failModelSwitchPending(reason: string): void;
  onSelectModel(modelId: string): void;
};

type UsePanelModelSwitchInput = {
  panelBridge: CompanionPanelBridge | null;
  settings: ReminderSettings | null;
  settingsHydrated: boolean;
  settingsRef: MutableRefObject<ReminderSettings | null>;
};

/**
 * usePanelModelSwitch 管理人物模型切换的 pending 与错误状态。
 *
 * Args:
 *   input: panel bridge、设置快照和设置 ref。
 *
 * Returns:
 *   选择模型命令、pending 模型与请求编号、错误信息，以及成功和失败收口回调。
 */
export function usePanelModelSwitch(input: UsePanelModelSwitchInput): PanelModelSwitchRuntime {
  const [pendingModelId, setPendingModelId] = useState<string | null>(null);
  const [modelSwitchError, setModelSwitchError] = useState<string | null>(null);
  const pendingModelIdRef = useRef<string | null>(null);
  const pendingRequestIdRef = useRef<string | null>(null);
  const acceptanceTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelAcceptanceWait = useCallback(() => {
    if (acceptanceTimeoutRef.current === null) return;
    clearTimeout(acceptanceTimeoutRef.current);
    acceptanceTimeoutRef.current = null;
  }, []);

  useEffect(() => cancelAcceptanceWait, [cancelAcceptanceWait]);

  useEffect(() => {
    const disposeModelSwitch = input.panelBridge?.onModelSwitchStateChanged(state => {
      // 重试后，旧请求的迟到回执不能解锁新请求，也不能覆盖新请求的错误提示。
      const currentPendingModelId = pendingModelIdRef.current;
      if (currentPendingModelId === null) {
        // 启动时无法恢复旧人物的通知没有请求编号；它只展示错误，不能重新获取切换锁。
        if (state.status === "failed" && state.requestId === undefined) setModelSwitchError(state.reason);
        return;
      }
      if (currentPendingModelId !== state.modelId || state.requestId !== pendingRequestIdRef.current) return;
      cancelAcceptanceWait();

      /**
       * panel 只消费 avatar 侧投影过来的最小切换状态。
       * switching 用来锁住列表；failed 负责解锁并展示失败原因；
       * succeeded 等待设置事件或有界权威读取确认，避免回跳到旧选中态。
       */
      startTransition(() => {
        if (state.status === "switching") {
          pendingModelIdRef.current = state.modelId;
          setPendingModelId(state.modelId);
          setModelSwitchError(null);
          return;
        }

        if (state.status === "failed") {
          pendingModelIdRef.current = null;
          pendingRequestIdRef.current = null;
          setPendingModelId(null);
          setModelSwitchError(state.reason);
        }
      });
    });

    return () => {
      disposeModelSwitch?.();
    };
  }, [cancelAcceptanceWait, input.panelBridge]);

  const onSelectModel = useCallback(
    (modelId: string) => {
      /**
       * panel 只发出切换意图，不执行切换本身。
       * 因此这里严格拦截三类无效点击：设置尚未水合、已有切换在路上、重复点击当前模型。
       */
      if (
        !input.panelBridge ||
        !input.settingsHydrated ||
        !input.settingsRef.current ||
        pendingModelIdRef.current ||
        modelId === input.settingsRef.current.selected_live2d_model_id
      ) {
        return;
      }

      // 先锁住同步引用，React 尚未更新按钮时的第二次点击也不能提交另一条请求。
      pendingModelIdRef.current = modelId;
      const requestId = crypto.randomUUID();
      pendingRequestIdRef.current = requestId;
      setPendingModelId(modelId);
      setModelSwitchError(null);
      cancelAcceptanceWait();
      acceptanceTimeoutRef.current = setTimeout(() => {
        acceptanceTimeoutRef.current = null;
        if (pendingRequestIdRef.current !== requestId) return;
        pendingModelIdRef.current = null;
        pendingRequestIdRef.current = null;
        setPendingModelId(null);
        setModelSwitchError("人物切换没有响应，请稍后重试或重新打开人物面板。");
      }, MODEL_SWITCH_ACCEPTANCE_TIMEOUT_MS);
      input.panelBridge.requestModelSwitch(modelId, requestId);
    },
    [cancelAcceptanceWait, input.panelBridge, input.settingsHydrated, input.settingsRef]
  );

  const clearModelSwitchPending = useCallback(() => {
    cancelAcceptanceWait();
    pendingModelIdRef.current = null;
    pendingRequestIdRef.current = null;
    setPendingModelId(null);
    setModelSwitchError(null);
  }, [cancelAcceptanceWait]);

  const failModelSwitchPending = useCallback((reason: string) => {
    cancelAcceptanceWait();
    pendingModelIdRef.current = null;
    pendingRequestIdRef.current = null;
    setPendingModelId(null);
    setModelSwitchError(reason);
  }, [cancelAcceptanceWait]);

  return {
    pendingModelId,
    pendingModelIdRef,
    pendingRequestIdRef,
    modelSwitchError,
    clearModelSwitchPending,
    failModelSwitchPending,
    onSelectModel
  };
}
