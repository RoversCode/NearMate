import { startTransition, useEffect, type MutableRefObject } from "react";
import { classifyLiveKitSessionError, LiveKitSessionRequestError } from "../../../shared/liveKitSessionRequest";
import { AssistantAudioPlayback } from "../lib/assistantAudioPlayback";
import { LiveKitRoomClient } from "../lib/livekitRoom";
import type { AudioDevicesChangeRequestedEvent } from "../types/events";
import type { AvatarBubbleEvent } from "./avatarBubbleState";
import type { AvatarPlayoutDeliveryCoordinator } from "./playoutDeliveryCoordinator";
import {
  fetchLiveKitSession,
  isAbortError,
  normalizeRendererLiveKitSession,
  reportAudioDeviceApplyResult,
  type LiveKitSessionPayload
} from "./avatarRuntimeSession";

type CompanionAvatarLiveKitBridge = {
  /**
   * 通过 Electron preload 请求 main 进程代理创建 LiveKit session。
   * 核心输出是 renderer 进房所需的地址与 token；边界异常由调用方统一转成诊断事件。
   */
  createLiveKitSession?(): Promise<LiveKitSessionPayload>;
  /** 让主进程中止尚未完成的会话请求，并收回该请求可能启动的 AEC。 */
  cancelLiveKitSession?(): Promise<void>;
  reportDiagnostic(level: "info" | "warn" | "error", message: string, details?: unknown): void;
  runtime: {
    controlGateway?: {
      websocketUrl: string;
      httpBaseUrl: string;
    };
  };
};

type UseAvatarLiveKitSessionInput = {
  audioDeviceSessionReloadRef: MutableRefObject<((payload: AudioDevicesChangeRequestedEvent["payload"]) => void) | null>;
  avatarBridge?: CompanionAvatarLiveKitBridge;
  assistantAudioPlayback?: AssistantAudioPlayback;
  dispatch(action: AvatarBubbleEvent): void;
  latestAudioDeviceChangeRequestIdRef: MutableRefObject<string | null>;
  onAssistantPlaybackElementChangedRef: MutableRefObject<((element: HTMLMediaElement | null) => void) | undefined>;
  onLocalListeningHintChangedRef: MutableRefObject<((active: boolean) => void) | undefined>;
  playoutDeliveryCoordinatorRef: MutableRefObject<AvatarPlayoutDeliveryCoordinator | null>;
};

/**
 * 管理头像窗口的媒体连接，串行恢复瞬时故障并隔离旧设备请求。
 *
 * Args:
 *   input: preload bridge、气泡 dispatch、音频播放元素回调和重连引用。
 *
 * Returns:
 *   无返回值；通过播放回调、气泡和设备应用结果发布状态。
 */
export function useAvatarLiveKitSession({
  audioDeviceSessionReloadRef,
  avatarBridge,
  assistantAudioPlayback,
  dispatch,
  latestAudioDeviceChangeRequestIdRef,
  onAssistantPlaybackElementChangedRef,
  onLocalListeningHintChangedRef,
  playoutDeliveryCoordinatorRef
}: UseAvatarLiveKitSessionInput): void {
  useEffect(() => {
    const liveKitRoom = new LiveKitRoomClient({
      assistantAudioPlayback: assistantAudioPlayback ?? new AssistantAudioPlayback(document, {
        onPlaybackElementChanged(element) {
          onAssistantPlaybackElementChangedRef.current?.(element);
        }
      })
    });
    let cancelled = false;
    let generation = 0;
    let activeAttempt: Promise<void> | null = null;
    let pendingDisconnect: Promise<void> | null = null;
    let activeController: AbortController | null = null;
    let restartPending = false;
    let needsDisconnect = false;
    let currentRequestId: string | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let failureCount = 0;
    let lastNoticeCode: string | null = null;
    /**
     * LiveKit session 与控制面共享同一个 HTTP 根地址。
     * 业务意图是让 avatar renderer 只依赖 preload 暴露的控制服务入口，不再持有旧音频 WebSocket 地址。
     */
    const httpBaseUrl =
      avatarBridge?.runtime.controlGateway?.httpBaseUrl ?? "http://127.0.0.1:8765";

    function requestLiveKitSession(signal: AbortSignal): Promise<unknown> {
      if (avatarBridge?.createLiveKitSession) {
        /**
         * 产品路径优先同步发起 preload/main 代理请求。
         * 业务意图是避免把 broker 调用排进微任务后，React cleanup 已运行的旧 effect 仍创建 sidecar/session。
         */
        return avatarBridge.createLiveKitSession();
      }

      /**
       * HTTP 边界仅服务非 Electron 或测试环境。
       * 这里继续绑定 AbortController，让组件卸载时能取消 pending 请求。
       */
      return fetchLiveKitSession(httpBaseUrl, signal);
    }

    async function reportAudioDeviceApplyResultSafely(
      requestId: string,
      status: "applied" | "failed",
      reason?: string
    ): Promise<void> {
      try {
        await reportAudioDeviceApplyResult(httpBaseUrl, requestId, status, reason);
      } catch {
        avatarBridge?.reportDiagnostic("warn", "avatar.audio_device_apply_result_failed", {
          message: "音频设备应用结果回传失败。"
        });
      }
    }

    function clearRetry(): void {
      if (retryTimer !== null) clearTimeout(retryTimer);
      retryTimer = null;
    }

    function restartSession(requestId?: string): void {
      generation += 1;
      currentRequestId = requestId;
      restartPending = true;
      needsDisconnect = true;
      activeController?.abort();
      cancelBrokerRequest();
      clearRetry();
      startPendingAttempt();
    }

    function startPendingAttempt(): void {
      if (cancelled || activeAttempt || !restartPending) return;
      restartPending = false;
      const attemptGeneration = generation;
      const requestId = currentRequestId;
      const controller = new AbortController();
      activeController = controller;
      // 取消会通过独立 IPC 到达 main；仍等待旧 broker 收口后才申请新会话，避免重叠 AEC。
      activeAttempt = connectLiveKitSession(attemptGeneration, requestId, controller.signal).finally(() => {
        activeAttempt = null;
        if (activeController === controller) activeController = null;
        startPendingAttempt();
      });
    }

    function cancelBrokerRequest(): void {
      if (!activeAttempt || !avatarBridge?.cancelLiveKitSession) return;
      void avatarBridge.cancelLiveKitSession().catch(() => {
        avatarBridge.reportDiagnostic("warn", "avatar.livekit_cancel_failed", { message: "旧语音连接取消请求未送达，正在等待原请求收口。" });
      });
    }

    async function connectLiveKitSession(attemptGeneration: number, requestId: string | undefined, signal: AbortSignal): Promise<void> {
      const isCurrent = () => !cancelled && generation === attemptGeneration && !signal.aborted;
      let connectionFailed = false;
      const recordFailure = (error: unknown) => {
        if (!isCurrent() || connectionFailed) return;
        connectionFailed = true;
        const failure = classifyLiveKitSessionError(error);
        playoutDeliveryCoordinatorRef.current?.markPlaybackUnavailable(failure.code, failure.message);
        avatarBridge?.reportDiagnostic("error", "avatar.livekit_connect_failed", {
          code: failure.code, message: failure.message, retryable: failure.retryable
        });
        if (lastNoticeCode !== failure.code) {
          lastNoticeCode = failure.code;
          startTransition(() => dispatch({
            type: "shell.avatar_bubble_changed",
            payload: { kind: "notice", text: failure.message, ttl_ms: 12000 }
          }));
        }
        if (requestId) void reportAudioDeviceApplyResultSafely(requestId, "failed", failure.message);
        if (!failure.retryable || retryTimer !== null) return;
        const delay = Math.min(30_000, 1000 * 2 ** Math.min(failureCount++, 5));
        retryTimer = setTimeout(() => {
          retryTimer = null;
          if (isCurrent()) restartSession(requestId);
        }, delay);
      };
      try {
        // 必须确认上一条媒体连接已释放，清理失败时仍保持阻断，不能叠出多个房间。
        if (needsDisconnect) {
          if (!pendingDisconnect) {
            const operation = liveKitRoom.disconnect();
            pendingDisconnect = operation;
            const clearPending = () => { if (pendingDisconnect === operation) pendingDisconnect = null; };
            void operation.then(clearPending, clearPending);
          }
          // 清理超时只结束本次等待；后续重试继续等同一操作，绝不叠加新连接。
          await waitForMediaConnection(pendingDisconnect, signal);
          if (!isCurrent()) return;
          needsDisconnect = false;
        }
        const session = await requestLiveKitSession(signal);
        /**
         * effect 已卸载后不能再启动真实媒体连接。
         * 这能避免 avatar 窗口关闭时，迟到的 session 响应重新打开麦克风或进入房间。
         */
        if (!isCurrent()) {
          return;
        }

        let uiSession;
        try {
          uiSession = normalizeRendererLiveKitSession(session);
        } catch {
          throw new LiveKitSessionRequestError("livekit_invalid_response");
        }
        const audioInputUnavailable = uiSession.audioInputStatus === "audio_unavailable";
        if (audioInputUnavailable) {
          startTransition(() => {
            dispatch({
              type: "shell.avatar_bubble_changed",
              payload: {
                kind: "notice",
                text: "麦克风不可用，去 Console 选择设备。",
                ttl_ms: 12000
              }
            });
          });
          avatarBridge?.reportDiagnostic("warn", "avatar.audio_input_unavailable", {
            message: "麦克风不可用，已继续连接 LiveKit UI 会话。"
          });
        }

        await waitForMediaConnection(liveKitRoom.connect(
          {
            url: uiSession.url,
            token: uiSession.connection.token,
            autoPublishMicrophone: false
          },
          {
            onStatusChange(status, reason) {
              if (!isCurrent()) return;
              // SDK reason 可能含服务地址或凭据，只记录状态和我们自己的安全分类。
              avatarBridge?.reportDiagnostic("info", "avatar.livekit_status_changed", {
                status
              });
              if (status === "failed" || status === "disconnected") {
                recordFailure(new Error(reason ?? ""));
              }
            },
            onAssistantTrackSubscribed(identity) {
              if (!isCurrent() || connectionFailed) return;
              /**
               * 助手音轨订阅事件同样先上报诊断。
               * 后续真正消费远端音频时，可以从这里继续接入播放或可视化链路。
               */
              avatarBridge?.reportDiagnostic(
                "info",
                "avatar.livekit_assistant_track_subscribed",
                {
                  identity
                }
              );
            },
            onLocalListeningHintChanged(active) {
              if (!isCurrent() || connectionFailed) return;
              onLocalListeningHintChangedRef.current?.(active);
            },
            onAssistantPlaybackReady() {
              if (!isCurrent() || connectionFailed) return;
              playoutDeliveryCoordinatorRef.current?.markPlaybackReady();
            },
            onAssistantPlaybackUnavailable() {
              if (!isCurrent() || connectionFailed) return;
              playoutDeliveryCoordinatorRef.current?.markPlaybackUnavailable(
                "livekit_playback_failed", "语音播放失败，请检查扬声器设置。"
              );
            }
          }
        ), signal);

        if (!isCurrent() || connectionFailed) return;
        failureCount = 0;
        lastNoticeCode = null;
        if (!requestId) return;

        // 成功和失败回执都不占有媒体建连锁，避免回传故障卡住下一次设备切换。
        void reportAudioDeviceApplyResultSafely(
          requestId,
          audioInputUnavailable ? "failed" : "applied",
          audioInputUnavailable ? "麦克风不可用，AEC sidecar 未能启动。" : undefined
        );
      } catch (error) {
        /**
         * 组件卸载后的 session 失败属于预期取消路径。
         * 业务意图是关闭头像窗口后不再输出误导性的 LiveKit 连接失败诊断。
         */
        if (!isCurrent()) {
          return;
        }

        // 当前代际若接到旧 broker 的取消错误，仍需恢复；自身取消已由上面的代际检查过滤。
        recordFailure(isAbortError(error) ? new LiveKitSessionRequestError("livekit_connection_failed") : error);
      }
    }

    audioDeviceSessionReloadRef.current = (payload) => {
      latestAudioDeviceChangeRequestIdRef.current = payload.request_id;
      failureCount = 0;
      restartSession(payload.request_id);
    };
    restartPending = true;
    startPendingAttempt();

    return () => {
      cancelled = true;
      generation += 1;
      clearRetry();
      audioDeviceSessionReloadRef.current = null;
      latestAudioDeviceChangeRequestIdRef.current = null;
      activeController?.abort();
      cancelBrokerRequest();
      void (pendingDisconnect ?? liveKitRoom.disconnect()).catch(() => {
        /**
         * 卸载清理失败不能变成未观察 rejection。
         * 这里使用独立诊断事件，避免和启动期 connect_failed 混在一起。
         */
        avatarBridge?.reportDiagnostic("warn", "avatar.livekit_disconnect_failed", {
          message: "语音连接清理失败。"
        });
      });
    };
  }, [avatarBridge, assistantAudioPlayback]);
}

export type { CompanionAvatarLiveKitBridge };

/** 在取消或 SDK 长时间无响应时结束等待，下一代仍须完成 disconnect 才能连接。 */
async function waitForMediaConnection(connection: Promise<void>, signal: AbortSignal): Promise<void> {
  let rejectWait!: (error: Error) => void;
  const cancelled = new Promise<never>((_resolve, reject) => { rejectWait = reject; });
  const abort = () => rejectWait(new DOMException("连接已取消。", "AbortError"));
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  const timeout = setTimeout(() => rejectWait(new LiveKitSessionRequestError("livekit_request_timeout")), 20_000);
  try {
    await Promise.race([connection, cancelled]);
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", abort);
  }
}
