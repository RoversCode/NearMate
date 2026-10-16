import { useEffect, useMemo, useRef, useState } from "react";
import { ControlSocket } from "../lib/controlSocket";
import type { ReminderSettings } from "../types/events";
import {
  getPanelBridge,
  getPanelModelCatalog
} from "./panelBridge";
import { usePanelControlConnection } from "./usePanelControlConnection";
import { usePanelModelSwitch } from "./usePanelModelSwitch";
import type { Live2dModelCatalogEntry } from "../../../shared/live2dModelTypes";

/**
 * PanelRuntime 是人物选择面板的唯一运行时出口。
 */
export type PanelRuntime = {
  modelCatalog: readonly Live2dModelCatalogEntry[];
  settingsHydrated: boolean;
  selectedModelId: string | null;
  pendingModelId: string | null;
  modelSwitchError: string | null;
  resetAvatarLayout(): void;
  onSelectModel(modelId: string): void;
};

/**
 * usePanelRuntime 负责水合人物设置并处理人物切换。
 */
export function usePanelRuntime(): PanelRuntime {
  const [settings, setSettings] = useState<ReminderSettings | null>(null);
  const [settingsHydrated, setSettingsHydrated] = useState(false);
  const controlSocketRef = useRef<ControlSocket | null>(null);
  const settingsRef = useRef<ReminderSettings | null>(null);
  const panelBridge = useMemo(() => getPanelBridge(), []);

  useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);

  const modelSwitch = usePanelModelSwitch({
    panelBridge,
    settings,
    settingsHydrated,
    settingsRef
  });
  usePanelControlConnection({
    controlSocketRef,
    panelBridge,
    pendingModelIdRef: modelSwitch.pendingModelIdRef,
    pendingRequestIdRef: modelSwitch.pendingRequestIdRef,
    setSettings,
    setSettingsHydrated,
    clearModelSwitchPending: modelSwitch.clearModelSwitchPending,
    failModelSwitchPending: modelSwitch.failModelSwitchPending
  });

  return {
    modelCatalog: getPanelModelCatalog(panelBridge),
    settingsHydrated,
    selectedModelId: settings?.selected_live2d_model_id ?? null,
    pendingModelId: modelSwitch.pendingModelId,
    modelSwitchError: modelSwitch.modelSwitchError,
    resetAvatarLayout() {
      /**
       * 位置重置只表达当前人物恢复默认布局的意图。
       * 具体清理哪条记录和如何重算窗口位置，都收敛在主进程。
       */
      panelBridge?.resetAvatarLayout?.();
    },
    onSelectModel: modelSwitch.onSelectModel
  };
}
