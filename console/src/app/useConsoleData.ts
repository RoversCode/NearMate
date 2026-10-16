import { useEffect, useRef, useState } from "react";

import { type AudioDeviceApplyResult } from "../api/contracts/audioDevices";
import { isBootstrapStatus, type BootstrapStatus } from "../api/contracts/bootstrap";
import {
  isConsoleSettings,
  normalizeConsoleSettings,
  type ConsoleSettings
} from "../api/contracts/settings";
import {
  isShortcutStatusPayload,
  type ShortcutStatusPayload
} from "../api/contracts/shortcutStatus";
import { fetchActiveCharacterCard, fetchCharacterCards } from "../api/services/characterCards";
import { fetchBootstrapStatus } from "../api/services/bootstrap";
import { fetchLlmConfig, fetchSupportedLlmModels } from "../api/services/llm";
import { fetchLogs } from "../api/services/records";
import { fetchSettings } from "../api/services/settings";
import { fetchShortcutStatus } from "../api/services/shortcutStatus";
import { fetchStreamVoxRuntime } from "../api/services/streamvox";
import { fetchWakeResponseConfig } from "../api/services/wakeResponse";
import { connectControlSocket } from "../lib/controlSocket";
import {
  initialConsoleData,
  type ConsoleData,
  type ConsoleLoadState
} from "./consoleTypes";

/**
 * 提供 Console 管理数据和会话可访问性，语音暂停时不会把初始化状态伪造成成功。
 *
 * Args:
 *   不适用；这是 useConsoleData 返回的数据控制接口。
 * Returns:
 *   权威 Bootstrap 状态、当前快照、管理页面资格和刷新动作。
 */
export type ConsoleDataController = {
  consoleData: ConsoleData;
  setConsoleData: React.Dispatch<React.SetStateAction<ConsoleData>>;
  bootstrapStatus: BootstrapStatus | null;
  setBootstrapStatus: React.Dispatch<React.SetStateAction<BootstrapStatus | null>>;
  isConsoleAvailable: boolean;
  loadState: ConsoleLoadState;
  isLoadingCharacterCard: boolean;
  characterCardLoadErrorMessage: string | null;
  streamVoxLoadErrorMessage: string | null;
  audioDeviceApplyResult: AudioDeviceApplyResult | null;
  loadConsoleData: (baseUrl: string, shouldApplyResult?: () => boolean) => Promise<void>;
  loadStreamVoxRuntimeData: (baseUrl: string, shouldApplyResult?: () => boolean) => Promise<void>;
  loadCharacterCardData: (baseUrl: string) => Promise<void>;
  handleSelectCharacterCard: (cardId: string) => Promise<void>;
};

/**
 * 管理控制台基础数据、首屏加载和控制 socket 事件。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   App 壳层和业务动作共享的数据状态与加载函数。
 */
export function useConsoleData(baseUrl: string): ConsoleDataController {
  const [consoleData, setConsoleData] = useState<ConsoleData>(initialConsoleData);
  const [bootstrapStatus, setBootstrapStatus] = useState<BootstrapStatus | null>(null);
  const [loadState, setLoadState] = useState<ConsoleLoadState>({
    status: "loading",
    errorMessage: null
  });
  const [isLoadingCharacterCard, setIsLoadingCharacterCard] = useState(false);
  const [characterCardLoadErrorMessage, setCharacterCardLoadErrorMessage] = useState<string | null>(null);
  const [streamVoxLoadErrorMessage, setStreamVoxLoadErrorMessage] = useState<string | null>(null);
  const [audioDeviceApplyResult, setAudioDeviceApplyResult] = useState<AudioDeviceApplyResult | null>(null);
  const activeDataBaseUrl = useRef(baseUrl);
  const consoleDataLoadedRef = useRef(false);
  // 首屏加载请求序号：每次发起都递增，只有最新序号的迟到结果允许写状态。
  const consoleDataLoadRequestRef = useRef(0);
  // bootstrap 状态没有 revision，用落地序号记录 WebSocket 已应用的最新状态，慢 HTTP 结果据此放弃覆盖。
  const bootstrapStatusSocketSequenceRef = useRef(0);
  // 已进入管理的用户准备语音时仍能管理设置；事件门禁和页面门禁使用同一规则。
  const consoleAvailableRef = useRef<boolean | null>(null);
  const bootstrapVoiceReadyRef = useRef<boolean | null>(null);
  // StreamVox 独立加载不属于首屏 Promise，需要自己的序号才能在 Bootstrap 失效时一并作废。
  const streamVoxLoadRequestRef = useRef(0);
  const characterCardSelectionRequestRef = useRef(0);

  /**
   * 独立读取 StreamVox runtime 状态。
   *
   * Args:
   *   requestBaseUrl: 控制面 HTTP 根地址。
   *   shouldApplyResult: 组件卸载后阻止异步结果写入状态。
   *
   * Returns:
   *   完成后写入 StreamVox 页面所需 runtime。
   */
  async function loadStreamVoxRuntimeData(
    requestBaseUrl: string,
    shouldApplyResult: () => boolean = () => true
  ): Promise<void> {
    const requestId = ++streamVoxLoadRequestRef.current;
    const canApplyResult = (): boolean => (
      requestId === streamVoxLoadRequestRef.current && shouldApplyResult()
    );
    try {
      const streamVoxRuntime = await fetchStreamVoxRuntime(requestBaseUrl);

      if (!canApplyResult()) {
        return;
      }

      setConsoleData((currentData) => ({
        ...currentData,
        streamVoxRuntime
      }));
      setStreamVoxLoadErrorMessage(null);
    } catch (error) {
      if (!canApplyResult()) {
        return;
      }

      setStreamVoxLoadErrorMessage(error instanceof Error ? error.message : "未知 StreamVox 状态读取错误");
    }
  }

  /**
   * 读取 Bootstrap 状态，并保留已进入管理的会话在语音准备期间使用的设置。
   *
   * Args:
   *   requestBaseUrl: 控制面 HTTP 根地址。
   *   shouldApplyResult: 组件卸载后阻止异步结果写入状态。
   *   socketBootstrapStatus: 控制通道刚推送的权威状态；提供时不再用可能滞后的 HTTP 状态覆盖它。
   *
   * Returns:
   *   首次未初始化时只读取引导数据；已初始化时加载管理数据，后续仅语音未就绪时保留已有设置。
   *   严格模式重放 effect 或 WebSocket 事件触发的重取都会发起新请求；只有最新请求的结果生效，
   *   较早请求的迟到结果直接丢弃，因此清理阶段不会造成"丢弃且无重发"的死锁。
   */
  async function loadConsoleData(
    requestBaseUrl: string,
    shouldApplyResult: () => boolean = () => true,
    socketBootstrapStatus?: BootstrapStatus,
  ): Promise<void> {
    const requestId = ++consoleDataLoadRequestRef.current;
    const socketStatusSequenceAtStart = bootstrapStatusSocketSequenceRef.current;
    const canApplyResult = (): boolean => (
      requestId === consoleDataLoadRequestRef.current && shouldApplyResult()
    );

    /**
     * 回写 HTTP 抓取的 bootstrap 状态。
     *
     * Args:
     *   nextBootstrapStatus: 本次请求开始以来未经 WebSocket 事件刷新的抓取结果。
     *
     * Returns:
     *   抓取期间已有更新的 WebSocket 状态落地时放弃覆盖，否则写入状态。
     */
    const applyHttpBootstrapStatus = (nextBootstrapStatus: BootstrapStatus): void => {
      if (
        socketBootstrapStatus === undefined
        && bootstrapStatusSocketSequenceRef.current === socketStatusSequenceAtStart
      ) {
        consoleAvailableRef.current = isConsoleAvailableForStatus(nextBootstrapStatus, consoleDataLoadedRef.current);
        bootstrapVoiceReadyRef.current = nextBootstrapStatus.voice.is_ready;
        setBootstrapStatus(nextBootstrapStatus);
      }
    };

    try {
      const [nextBootstrapStatus, llmConfig, supportedLlmModels] = await Promise.all([
        socketBootstrapStatus === undefined
          ? fetchBootstrapStatus(requestBaseUrl)
          : Promise.resolve(socketBootstrapStatus),
        fetchLlmConfig(requestBaseUrl),
        fetchSupportedLlmModels(requestBaseUrl)
      ]);

      if (!canApplyResult()) {
        return;
      }

      if (!nextBootstrapStatus.is_initialized && !isConsoleAvailableForStatus(nextBootstrapStatus, consoleDataLoadedRef.current)) {
        // 首次初始化前仍要展示 LLM 配置模块，但必须短路普通控制台数据加载，避免缺少密钥时触发历史、设置和观测接口。
        applyHttpBootstrapStatus(nextBootstrapStatus);
        consoleDataLoadedRef.current = false;
        setConsoleData({
          ...initialConsoleData,
          llmConfig,
          supportedLlmModels
        });
        setLoadState({
          status: "ready",
          errorMessage: null
        });
        return;
      }

      if (!nextBootstrapStatus.is_initialized && consoleDataLoadedRef.current) {
        // 已进入控制台的用户切换语音时保留设置快照，只收起旧 StreamVox runtime，等待试听确认后恢复。
        applyHttpBootstrapStatus(nextBootstrapStatus);
        streamVoxLoadRequestRef.current += 1;
        setConsoleData(currentData => ({ ...currentData, llmConfig, supportedLlmModels, streamVoxRuntime: null }));
        setLoadState({ status: "ready", errorMessage: null });
        return;
      }

      // 首次加载已完成过初始化的用户，或刷新时正处于语音重配置，都要重新读取普通控制台的基础快照。
      applyHttpBootstrapStatus(nextBootstrapStatus);
      setLoadState({
        status: "loading",
        errorMessage: null
      });

      // 设置和运行日志是控制台外壳的基础数据；角色卡唤醒回复进入角色卡页后再按卡读取。
      const [settings, shortcutStatus, logs] = await Promise.all([
        fetchSettings(requestBaseUrl),
        fetchShortcutStatus(requestBaseUrl),
        fetchLogs(requestBaseUrl)
      ]);

      if (!canApplyResult()) {
        return;
      }

      setConsoleData((currentData) => ({
        settings: preferNewerSettings(currentData.settings, settings),
        // 初始 HTTP 快照和控制 WebSocket 没有顺序保证。HTTP 较早开始但较晚
        // 返回时，不能把已经收到的更新版本状态退回成旧状态，否则页面会一直显示
        // “待检测”，直到下一次快捷键配置变更。
        shortcutStatus: preferNewerShortcutStatus(currentData.shortcutStatus, shortcutStatus),
        llmConfig,
        supportedLlmModels,
        characterCards: null,
        activeCharacterCard: null,
        streamVoxRuntime: null,
        wakeResponseConfig: initialConsoleData.wakeResponseConfig,
        logs
      }));
      setLoadState({
        status: "ready",
        errorMessage: null
      });
      consoleDataLoadedRef.current = true;

      void loadStreamVoxRuntimeData(requestBaseUrl, canApplyResult);
    } catch (error) {
      if (!canApplyResult()) {
        return;
      }

      // 本地控制台的失败信息要直接暴露，方便区分后端未启动、接口未挂载和响应格式不匹配。
      setLoadState({
        status: "failed",
        errorMessage: error instanceof Error ? error.message : "未知控制台加载错误"
      });
    }
  }

  /**
   * 按需读取角色卡列表和当前激活角色卡。
   *
   * Args:
   *   requestBaseUrl: 控制面 HTTP 根地址。
   *
   * Returns:
   *   写入角色卡卡槽和当前编辑卡。
   */
  async function loadCharacterCardData(requestBaseUrl: string): Promise<void> {
    if (consoleData.characterCards !== null && consoleData.activeCharacterCard !== null) {
      return;
    }
    const requestId = ++characterCardSelectionRequestRef.current;
    setIsLoadingCharacterCard(true);
    setCharacterCardLoadErrorMessage(null);

    try {
      const [characterCards, activeCharacterCard] = await Promise.all([
        fetchCharacterCards(requestBaseUrl),
        fetchActiveCharacterCard(requestBaseUrl)
      ]);
      const wakeResponseConfig =
        activeCharacterCard.card === null
          ? initialConsoleData.wakeResponseConfig
          : await fetchWakeResponseConfig(requestBaseUrl, activeCharacterCard.card.meta.card_id);

      if (requestId !== characterCardSelectionRequestRef.current) {
        return;
      }
      setConsoleData((currentData) => ({
        ...currentData,
        characterCards,
        activeCharacterCard,
        wakeResponseConfig
      }));
    } catch (error) {
      if (requestId !== characterCardSelectionRequestRef.current) {
        return;
      }
      setCharacterCardLoadErrorMessage(error instanceof Error ? error.message : "未知角色卡读取错误");
    } finally {
      if (requestId === characterCardSelectionRequestRef.current) {
        setIsLoadingCharacterCard(false);
      }
    }
  }

  /**
   * 切换当前编辑卡时只读取对应唤醒回复配置。
   *
   * Args:
   *   cardId: 目标角色卡编号。
   *
   * Returns:
   *   完成后刷新当前唤醒回复配置。
   */
  async function handleSelectCharacterCard(cardId: string): Promise<void> {
    const requestId = ++characterCardSelectionRequestRef.current;
    setCharacterCardLoadErrorMessage(null);
    try {
      const nextConfig = await fetchWakeResponseConfig(baseUrl, cardId);
      if (requestId !== characterCardSelectionRequestRef.current) {
        return;
      }
      setConsoleData((currentData) => ({
        ...currentData,
        wakeResponseConfig: nextConfig
      }));
    } catch (error) {
      if (requestId !== characterCardSelectionRequestRef.current) {
        return;
      }
      setCharacterCardLoadErrorMessage(error instanceof Error ? error.message : "未知角色卡唤醒回复读取错误");
    }
  }

  useEffect(() => {
    if (activeDataBaseUrl.current !== baseUrl) {
      // 不同 Backend 的 revision 不在同一版本轴上。切换地址时先清空旧快照，
      // 后续 HTTP 与 WebSocket 才能只比较当前 Backend 的持久化版本。
      activeDataBaseUrl.current = baseUrl;
      consoleDataLoadRequestRef.current += 1;
      streamVoxLoadRequestRef.current += 1;
      characterCardSelectionRequestRef.current += 1;
      consoleDataLoadedRef.current = false;
      bootstrapStatusSocketSequenceRef.current = 0;
      consoleAvailableRef.current = null;
      bootstrapVoiceReadyRef.current = null;
      setConsoleData(initialConsoleData);
    }
    let isActive = true;

    void loadConsoleData(baseUrl, () => isActive);

    return () => {
      // React 严格模式和测试卸载都会重放 effect；清理标记可以阻止过期请求继续写入状态。
      isActive = false;
    };
  }, [baseUrl]);

  useEffect(() => {
    if (bootstrapStatus === null) return;
    const wasVoiceReady = bootstrapVoiceReadyRef.current;
    consoleAvailableRef.current = isConsoleAvailableForStatus(bootstrapStatus, consoleDataLoadedRef.current);
    bootstrapVoiceReadyRef.current = bootstrapStatus.voice.is_ready;
    if (bootstrapStatus.is_initialized && wasVoiceReady === false && consoleDataLoadedRef.current) {
      // 试听确认的 HTTP 响应也能恢复音色状态；成功推送偶尔丢失时，不能一直停在旧的空 runtime。
      void loadStreamVoxRuntimeData(baseUrl);
    }
  }, [bootstrapStatus, baseUrl]);

  useEffect(() => {
    const connection = connectControlSocket(baseUrl, {
      onMessage(message) {
        // Bootstrap 已明确失效后，旧连接上迟到的普通业务事件也不能重新填充已淘汰快照。
        if (
          message.type !== "bootstrap.status_changed"
          && consoleAvailableRef.current === false
        ) {
          return;
        }
        if (message.type === "settings.changed") {
          if (!isConsoleSettings(message.payload)) {
            return;
          }

          // 每个设置入口都会推进同一全局 revision。及时采用权威快照，
          // 可以避免普通设置表单在其它领域保存后继续提交过期版本。
          const settings = normalizeConsoleSettings(message.payload);
          setConsoleData((currentData) => ({
            ...currentData,
            settings: preferNewerSettings(currentData.settings, settings)
          }));
          return;
        }

        if (message.type === "bootstrap.status_changed") {
          if (!isBootstrapStatus(message.payload)) return;
          // WebSocket 推送即为最新状态，落地序号加一，让在途 HTTP 加载发现状态已更新后放弃回写。
          bootstrapStatusSocketSequenceRef.current += 1;
          const wasVoiceReady = bootstrapVoiceReadyRef.current;
          consoleAvailableRef.current = isConsoleAvailableForStatus(message.payload, consoleDataLoadedRef.current);
          bootstrapVoiceReadyRef.current = message.payload.voice.is_ready;
          setBootstrapStatus(message.payload);
          if (!message.payload.is_initialized) {
            if (consoleAvailableRef.current) {
              // 已有管理快照不因准备语音而失效；旧音色状态则必须收起，避免误显示为仍在运行。
              streamVoxLoadRequestRef.current += 1;
              setConsoleData(currentData => ({ ...currentData, streamVoxRuntime: null }));
              setStreamVoxLoadErrorMessage(null);
              return;
            }
            // Bootstrap 失效意味着普通控制台快照已经不再可信；
            // 先使所有在途请求失效，再重新读取初始化页需要的最小数据。
            consoleDataLoadRequestRef.current += 1;
            streamVoxLoadRequestRef.current += 1;
            characterCardSelectionRequestRef.current += 1;
            consoleDataLoadedRef.current = false;
            setConsoleData(initialConsoleData);
            setLoadState({
              status: "loading",
              errorMessage: null
            });
            setIsLoadingCharacterCard(false);
            setCharacterCardLoadErrorMessage(null);
            setStreamVoxLoadErrorMessage(null);
            setAudioDeviceApplyResult(null);
            void loadConsoleData(baseUrl, undefined, message.payload);
            return;
          }
          if (message.payload.is_initialized && !consoleDataLoadedRef.current) {
            // 先同步关闭普通页面的按需加载门禁，避免角色卡请求抢在完整快照之前完成后又被清空。
            setLoadState({
              status: "loading",
              errorMessage: null
            });
            void loadConsoleData(baseUrl, undefined, message.payload);
            return;
          }
          if (wasVoiceReady === false) {
            // 模型切换完成后重新读取当前音色；旧 runtime 不能代表新模型。
            void loadStreamVoxRuntimeData(baseUrl);
          }
          return;
        }

        if (message.type === "settings.shortcut_status_changed") {
          if (!isShortcutStatusPayload(message.payload)) {
            return;
          }

          const shortcutStatus = message.payload;
          setConsoleData((currentData) => ({
            ...currentData,
            // 旧连接或重连期间的 WebSocket 事件也可能晚到；沿用同一单调
            // 合并规则，避免运行状态回退到旧 revision 或旧检测时间。
            shortcutStatus: preferNewerShortcutStatus(currentData.shortcutStatus, shortcutStatus)
          }));
          return;
        }

        if (message.type !== "audio.devices_apply_result") {
          return;
        }

        if (!isAudioDeviceApplyResult(message.payload)) {
          return;
        }

        setAudioDeviceApplyResult(message.payload);
      }
    });

    return () => {
      connection.close();
    };
  }, [baseUrl]);

  return {
    consoleData,
    setConsoleData,
    bootstrapStatus,
    setBootstrapStatus,
    isConsoleAvailable: bootstrapStatus !== null && isConsoleAvailableForStatus(bootstrapStatus, consoleDataLoadedRef.current),
    loadState,
    isLoadingCharacterCard,
    characterCardLoadErrorMessage,
    streamVoxLoadErrorMessage,
    audioDeviceApplyResult,
    loadConsoleData,
    loadStreamVoxRuntimeData,
    loadCharacterCardData,
    handleSelectCharacterCard,
  };
}

/**
 * 判断当前会话能否继续使用管理页面，不改变 Backend 的初始化真相。
 *
 * Args:
 *   status: Backend 已校验的 Bootstrap 状态。
 *   hasLoadedConsoleData: 当前 Backend 的完整管理快照是否曾经成功载入。
 *
 * Returns:
 *   已初始化，或已有管理会话仅因语音未就绪而暂停时返回 true；首次初始化、LLM 失效仍返回 false。
 */
function isConsoleAvailableForStatus(status: BootstrapStatus, hasLoadedConsoleData: boolean): boolean {
  return status.is_initialized || (
    status.llm.is_ready && !status.voice.is_ready &&
    (hasLoadedConsoleData || status.voice.has_confirmed_voice === true)
  );
}

/**
 * 在同一 Backend 的 HTTP 和 WebSocket 快照之间保留较新版本。
 *
 * Args:
 *   currentSettings: 当前页面已经采用的 Console 设置投影。
 *   candidateSettings: 刚从 HTTP 或控制通道通过校验的候选投影。
 *
 * Returns:
 *   revision 更大的快照；版本相同时采用刚到达的权威候选。
 */
function preferNewerSettings(
  currentSettings: ConsoleSettings | null,
  candidateSettings: ConsoleSettings
): ConsoleSettings {
  if (
    currentSettings !== null
    && currentSettings.settings_revision > candidateSettings.settings_revision
  ) {
    return currentSettings;
  }
  return candidateSettings;
}

/**
 * 合并快捷键状态快照，并按每个快捷键的 settings revision 丢弃迟到结果。
 *
 * Args:
 *   currentStatus: 页面已经采用的快捷键状态；可能来自 WebSocket。
 *   candidateStatus: 本次 HTTP 或 WebSocket 请求刚拿到的状态。
 *
 * Returns:
 *   每个快捷键保留更高版本的状态；同版本只接受 checked_at 更晚的快照，
 *   避免迟到的 HTTP 初始状态覆盖 WebSocket 已确认的实时状态。
 */
function preferNewerShortcutStatus(
  currentStatus: ShortcutStatusPayload | null,
  candidateStatus: ShortcutStatusPayload
): ShortcutStatusPayload {
  if (currentStatus === null) {
    return candidateStatus;
  }

  const currentById = new Map(
    currentStatus.items.map((item) => [item.shortcut_id, item])
  );
  const candidateById = new Map(
    candidateStatus.items.map((item) => [item.shortcut_id, item])
  );
  const mergedItems = new Map(currentById);

  // 以快捷键编号逐项合并，兼容未来的增量状态上报，也避免一个旧的完整 HTTP
  // 响应覆盖 WebSocket 已经确认的新版本。
  for (const [shortcutId, candidateItem] of candidateById) {
    const currentItem = currentById.get(shortcutId);
    if (currentItem === undefined || isShortcutStatusCandidateNewer(currentItem, candidateItem)) {
      mergedItems.set(shortcutId, candidateItem);
    }
  }

  return { items: [...mergedItems.values()] };
}

/**
 * 判断候选快捷键状态是否严格更新。
 *
 * 同一 settings revision 内，WebSocket 和初始 HTTP 可能返回不同时间点的
 * 检测结果。checked_at 为空代表 Backend 尚未收到桌面端检测，必须留住已有
 * 的非空实时结果；时间无法解析或完全相同时保守保留当前结果。
 */
function isShortcutStatusCandidateNewer(
  currentItem: ShortcutStatusPayload["items"][number],
  candidateItem: ShortcutStatusPayload["items"][number]
): boolean {
  if (candidateItem.settings_revision !== currentItem.settings_revision) {
    return candidateItem.settings_revision > currentItem.settings_revision;
  }

  const currentCheckedAt = parseShortcutStatusTimestamp(currentItem.checked_at);
  const candidateCheckedAt = parseShortcutStatusTimestamp(candidateItem.checked_at);
  if (currentCheckedAt === null || candidateCheckedAt === null) {
    return currentCheckedAt === null && candidateCheckedAt !== null;
  }
  return candidateCheckedAt > currentCheckedAt;
}

/** 将快捷键状态时间转换为可比较的毫秒数；空值或非法值按未知处理。 */
function parseShortcutStatusTimestamp(value: string): number | null {
  if (value.trim().length === 0) {
    return null;
  }
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? null : timestamp;
}

function isAudioDeviceApplyResult(payload: unknown): payload is AudioDeviceApplyResult {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }

  const candidate = payload as Record<string, unknown>;
  return (
    typeof candidate.request_id === "string" &&
    candidate.request_id.trim().length > 0 &&
    (candidate.status === "applied" || candidate.status === "failed") &&
    (candidate.mode === "system_default" || candidate.mode === "manual") &&
    (candidate.reason === null || typeof candidate.reason === "string") &&
    typeof candidate.applied_at === "number"
  );
}
