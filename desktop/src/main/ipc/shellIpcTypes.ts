import type { BrowserWindow } from "electron";
import type { AvatarWindowInteractionController } from "../windows/avatarWindowInteraction";
import type { ChatWindowDragController } from "../windows/chatWindowDrag";
import type { ChatWindowMotionController } from "../windows/chatWindowMotion";
import type { ChatWindowResizeController } from "../windows/chatWindowResize";
import type { ChatWindowLifecycleOwner } from "../windows/chatWindowLifecycle";
import type { ManagedMaterialAssetStore } from "../services/managedMaterialAssetStore";
import type { MaterialCommandService } from "../services/materialCommandService";
import type { MaterialDeliveryFeedbackService } from "../services/materialDeliveryFeedbackService";
import type { TurnSubmissionService } from "../services/turnSubmissionService";
import type { EnvironmentSnapshotService } from "../services/environmentSnapshotService";
import type { AvatarSummonMode } from "../shell/shellTypes";
import type { PanelView } from "../../shared/shellNavigation";
import type { WindowIdentityRegistry } from "./windowIdentityRegistry";

/**
 * ShellIpcRuntime 是 IPC 处理器需要访问的运行时窗口壳。
 */
type ShellIpcRuntime = {
  avatarWindow: BrowserWindow;
  panelWindow: BrowserWindow;
  chatWindow: BrowserWindow;
  chatWindowMotion: ChatWindowMotionController;
  chatWindowDrag: ChatWindowDragController;
  chatWindowResize: ChatWindowResizeController;
  chatLifecycle?: ChatWindowLifecycleOwner;
  controller: {
    hoverEnter(): void;
    hoverLeave(): void;
    getAvatarSummonMode(): AvatarSummonMode;
    toggleAvatarSummonedVisibility(): AvatarSummonMode;
    openPanel(view: PanelView): void;
    startDrag(): void;
    endDrag(): void;
    startMaterialDrop(): void;
    endMaterialDrop(): void;
    closePanelFromUserRequest(): void;
    closePanelFromExternalBlur(): void;
    refreshPanelPlacement(): void;
  };
};

/**
 * AecSidecarSessionStarter 描述 livekit:create-session 后启动 sidecar 所需的窄接口。
 */
type AecSidecarSessionStarter = {
  start(input: {
    livekitUrl: string;
    roomName: string;
    identity: string;
    token: string;
    debugAudio: unknown;
    audioDeviceSelection: unknown;
  }): Promise<void>;
  stopAndWait?(): Promise<void>;
  stop?(): void;
};

/**
 * RegisterShellIpcHandlersInput 是注册桌面壳 IPC 所需的全部依赖。
 */
export type RegisterShellIpcHandlersInput = {
  windowIdentities: WindowIdentityRegistry;
  getRuntime(): ShellIpcRuntime | null;
  // 首显交接只由 bootstrap 管理，renderer IPC 不需要也不能持有这些生命周期能力。
  avatarInteraction: Omit<AvatarWindowInteractionController, "holdFirstShow" | "releaseFirstShow">;
  aecSidecarSupervisor: AecSidecarSessionStarter;
  turnSubmission: TurnSubmissionService;
  environmentSnapshot: Pick<EnvironmentSnapshotService, "prepareTextSnapshot">;
  materialCommands: MaterialCommandService;
  materialFeedback: MaterialDeliveryFeedbackService;
  managedMaterialAssetStore: ManagedMaterialAssetStore;
};
