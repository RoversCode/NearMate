import { startTransition, useEffect, type MutableRefObject } from "react";
import { ControlSocket } from "../lib/controlSocket";
import type { ReminderSettings } from "../types/events";
import type { CompanionPanelBridge } from "./panelBridge";

type UsePanelControlConnectionInput = {
  controlSocketRef: MutableRefObject<ControlSocket | null>;
  panelBridge: CompanionPanelBridge | null;
  pendingModelIdRef: MutableRefObject<string | null>;
  pendingRequestIdRef: MutableRefObject<string | null>;
  setSettings(settings: ReminderSettings | null): void;
  setSettingsHydrated(hydrated: boolean): void;
  clearModelSwitchPending(): void;
  failModelSwitchPending(reason: string): void;
};

// 成功回执只证明 Avatar 已提交；最多三次权威读取确认，单次两秒，避免网络故障永久锁住列表。
const SETTINGS_CONFIRMATION_ATTEMPTS = 3;
const SETTINGS_CONFIRMATION_TIMEOUT_MS = 2000;
const SETTINGS_CONFIRMATION_RETRY_DELAY_MS = 500;

/**
 * 管理人物面板的设置水合和实时设置订阅。
 *
 * @param input 控制连接、人物切换状态与设置写入回调。
 * @returns 不返回值；通过回调写入权威设置或同步失败，忽略事件之后的初始读取，并在卸载时清理连接和等待。
 */
export function usePanelControlConnection(input: UsePanelControlConnectionInput): void {
  useEffect(() => {
    let disposed = false;
    let hasReceivedSettingsSnapshot = false;
    let confirmationGeneration = 0;
    const confirmationTimers = new Set<ReturnType<typeof setTimeout>>();
    const confirmationAborts = new Set<AbortController>();
    const controlSocket = new ControlSocket({
      websocketUrl: input.panelBridge?.runtime.controlGateway.websocketUrl,
      httpBaseUrl: input.panelBridge?.runtime.controlGateway.httpBaseUrl,
      diagnosticsBridge: input.panelBridge ?? undefined
    });
    input.controlSocketRef.current = controlSocket;

    void controlSocket.fetchSettings().then(snapshot => {
      // 事件已经给出当前人物时，旧 HTTP 和共享默认回退都没有覆盖它的资格。
      if (disposed || hasReceivedSettingsSnapshot) {
        return;
      }
      startTransition(() => {
        input.setSettings(snapshot);
        input.setSettingsHydrated(true);
      });
    }).catch(() => {
      if (!disposed && !hasReceivedSettingsSnapshot) {
        startTransition(() => input.setSettingsHydrated(true));
      }
    });

    const disposeSocket = controlSocket.connect({
      onEvent(event) {
        if (event.type !== "settings.changed") {
          return;
        }
        // 先记录已取得权威快照，再安排界面更新，避免迟到的初始化响应反向覆盖。
        hasReceivedSettingsSnapshot = true;
        startTransition(() => {
          input.setSettings(event.payload);
          input.setSettingsHydrated(true);
          if (input.pendingModelIdRef.current === event.payload.selected_live2d_model_id) {
            input.clearModelSwitchPending();
          }
        });
      },
      onStatusChange() {}
    });

    const disposeSwitchResult = input.panelBridge?.onModelSwitchStateChanged(state => {
      if (state.status !== "succeeded" || input.pendingModelIdRef.current !== state.modelId
        || state.requestId !== input.pendingRequestIdRef.current) return;
      const generation = ++confirmationGeneration;
      const isCurrent = () => !disposed && generation === confirmationGeneration
        && input.pendingRequestIdRef.current === state.requestId && input.pendingModelIdRef.current === state.modelId;

      // 只重试只读设置，不重复发起切换；默认回退值永远不能确认人物已经保存成功。
      void (async () => {
        for (let attempt = 0; attempt < SETTINGS_CONFIRMATION_ATTEMPTS; attempt += 1) {
          if (!isCurrent()) return;
          const abort = new AbortController();
          confirmationAborts.add(abort);
          let timeout: ReturnType<typeof setTimeout> | undefined;
          try {
            const snapshot = await Promise.race([
              controlSocket.fetchSettings({ requireAuthoritative: true, signal: abort.signal }),
              new Promise<never>((_resolve, reject) => {
                timeout = setTimeout(() => {
                  abort.abort();
                  reject(new Error("人物设置确认超时"));
                }, SETTINGS_CONFIRMATION_TIMEOUT_MS);
                confirmationTimers.add(timeout);
              })
            ]);
            if (!isCurrent()) return;
            if (snapshot.selected_live2d_model_id === state.modelId) {
              startTransition(() => {
                input.setSettings(snapshot);
                input.setSettingsHydrated(true);
                input.clearModelSwitchPending();
              });
              return;
            }
          } catch {
            // 网络失败、坏载荷和暂未追上的旧快照都等待下一次权威读取。
          } finally {
            if (timeout !== undefined) {
              clearTimeout(timeout);
              confirmationTimers.delete(timeout);
            }
            confirmationAborts.delete(abort);
          }
          if (!isCurrent()) return;
          if (attempt < SETTINGS_CONFIRMATION_ATTEMPTS - 1) {
            await new Promise<void>(resolve => {
              const timer = setTimeout(() => {
                confirmationTimers.delete(timer);
                resolve();
              }, SETTINGS_CONFIRMATION_RETRY_DELAY_MS);
              confirmationTimers.add(timer);
            });
          }
        }
        if (isCurrent()) input.failModelSwitchPending("无法确认人物设置，请稍后重试或重新打开人物面板。");
      })();
    });

    return () => {
      disposed = true;
      confirmationGeneration += 1;
      confirmationTimers.forEach(timer => clearTimeout(timer));
      confirmationAborts.forEach(abort => abort.abort());
      disposeSocket();
      disposeSwitchResult?.();
      input.controlSocketRef.current = null;
    };
  }, [
    input.clearModelSwitchPending,
    input.failModelSwitchPending,
    input.controlSocketRef,
    input.panelBridge,
    input.pendingModelIdRef,
    input.pendingRequestIdRef,
    input.setSettings,
    input.setSettingsHydrated
  ]);
}
