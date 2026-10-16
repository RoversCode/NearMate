import {
  calculatePanelPlacement,
  type PanelPlacement
} from "../services/panelPositioner";
import { initialShellState, reduceShellState } from "../shell/shellReducer";
import type { AvatarSummonMode, ShellState } from "../shell/shellTypes";
import type { AvatarWindowPresentationMode } from "../windows/avatarWindowPresentation";
import { resolveAvatarAnchorRegion } from "../windows/avatarAnchorGeometry";
import {
  PANEL_VIEW_SIZES,
  type PanelView
} from "../../shared/shellNavigation";

/**
 * Bounds 表示控制器在主进程内关心的基础窗口边界。
 * 入参由窗口端口返回，输出会继续传给面板定位服务。
 * 边界：这里只保留定位计算所需的最小几何字段，避免控制器耦合真实窗口对象的额外能力。
 */
type Bounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * AvatarWindowPort 定义角色窗需要暴露给控制器的最小命令集合。
 * 入参分别用于显示窗口、切换展示模式，以及读取当前角色边界。
 * 输出全部通过副作用完成；如果未来要接入真实 BrowserWindow，应由适配层实现这个端口。
 */
type AvatarWindowPort = {
  show(): void;
  applyPresentationMode(mode: AvatarWindowPresentationMode): void;
  getBounds(): Bounds;
  getPetBounds(): Bounds | null;
};

/**
 * PanelWindowPort 定义独立面板窗在控制器视角下的最小能力。
 * 入参覆盖隐藏、按坐标打开、以及请求聚焦三个动作。
 * 边界：控制器只下发这些命令，不参与面板窗创建、销毁或真实 Electron 生命周期管理。
 */
type PanelWindowPort = {
  hide(): void;
  openAt(
    placement: PanelPlacement,
    panelSize: { width: number; height: number }
  ): void;
  focus(): void;
};

/**
 * ShellControllerInput 定义控制器允许的两种互斥接线模式。
 * 第一种模式直接注入 getPanelPlacement，适合测试或更外层已经完成定位决策的场景；
 * 第二种模式注入 getDisplayBounds，由控制器串联纯 placement service 计算最终面板位置。
 * 边界：两种模式必须二选一，避免同时传两套依赖却只消费一套，导致调用方误判真实生效路径。
 */
type ShellControllerInput =
  | {
      avatarWindow: AvatarWindowPort;
      panelWindow: PanelWindowPort;
      getPanelPlacement(
        avatarAnchorBounds: Bounds,
        panelSize: { width: number; height: number }
      ): PanelPlacement;
    }
  | {
      avatarWindow: AvatarWindowPort;
      panelWindow: PanelWindowPort;
      getDisplayBounds(avatarAnchorBounds: Bounds): Bounds;
    };

/**
 * createShellController 负责把纯 reducer 状态与窗口端口命令串联起来。
 * 核心入参为角色窗端口、面板窗端口、面板尺寸，以及一套明确的面板定位依赖。
 * 输出为一组纯主进程控制命令，供更外层的事件接线代码调用。
 * 边界：该控制器不会创建真实窗口，只根据状态机结果同步两个 fake/real port 的行为。
 */
export function createShellController(input: ShellControllerInput) {
  /**
   * getPanelPlacement 统一抽象成一个局部函数，便于后续 openPanel 逻辑不关心接线模式差异。
   * 这里不再提供任何硬编码显示器边界猜测；如果调用方没给足定位依赖，会立即抛错暴露配置问题。
   */
  let getPanelPlacement: (
    avatarBounds: Bounds,
    panelSize: { width: number; height: number }
  ) => PanelPlacement;

  if ("getPanelPlacement" in input) {
    getPanelPlacement = input.getPanelPlacement;
  } else if ("getDisplayBounds" in input) {
    getPanelPlacement = (avatarBounds: Bounds, panelSize) =>
      calculatePanelPlacement({
        avatarAnchorBounds: avatarBounds,
        panelSize,
        displayBounds: input.getDisplayBounds(avatarBounds),
        gap: 16
      });
  } else {
    throw new Error(
      "Shell controller requires either getPanelPlacement or getDisplayBounds."
    );
  }

  /**
   * state 是控制器内部唯一的壳层状态快照，
   * 所有公开命令都必须先推进 reducer，再统一同步到窗口端口，避免命令分叉。
   */
  let state: ShellState = initialShellState;

  /**
   * syncShell 负责把当前纯状态投影成窗口层副作用。
   * 锚点状态只服务于面板定位，不代表透明头像窗口应该接管鼠标。
   * 只有人物悬停或拖拽这类真实 avatar 交互，才进入 interactive 展示模式。
   */
  function syncShell() {
    if (state.summonMode === "hidden") {
      input.avatarWindow.applyPresentationMode("hidden");
      input.panelWindow.hide();
      return;
    }

    const shouldReceiveAvatarPointer =
      state.avatarMode === "hover_revealed" ||
      state.avatarMode === "dragging" ||
      state.avatarMode === "material_drop_receiving";

    if (!shouldReceiveAvatarPointer && state.panelMode !== "closed") {
      input.avatarWindow.applyPresentationMode("passive");
    } else if (!shouldReceiveAvatarPointer && state.summonMode === "summoned_topmost") {
      input.avatarWindow.applyPresentationMode("summoned");
    } else if (!shouldReceiveAvatarPointer) {
      input.avatarWindow.applyPresentationMode("passive");
    } else if (state.panelMode === "closed") {
      input.avatarWindow.applyPresentationMode("interactive");
    } else {
      input.avatarWindow.applyPresentationMode("panel-adjacent-interactive");
    }

    // 当面板状态回到关闭态时，统一在这里下发隐藏命令，避免每个调用点重复判断。
    if (state.panelMode === "closed") {
      input.panelWindow.hide();
    }
  }

  /**
   * resolvePanelPlacementInput 优先使用人物可见边界作为侧边窗锚点。
   * 如果 renderer 尚未上报人物边界，则退回到角色窗边界，保证启动与异常路径仍可用。
   */
  function resolvePanelPlacementInput(): Bounds {
    const petBounds = input.avatarWindow.getPetBounds();

    if (!petBounds) {
      return input.avatarWindow.getBounds();
    }

    return resolveAvatarAnchorRegion(petBounds);
  }

  return {
    /**
     * start 建立启动期的最小窗口可见性基线。
     * 这里先显示角色窗，再依据初始状态同步壳层行为，确保启动阶段不会误展示面板。
     */
    start() {
      input.avatarWindow.show();
      syncShell();
    },

    /**
     * getAvatarSummonMode 暴露当前桌宠召回状态给主进程编排层读取。
     * 只读查询避免其他模块绕过 reducer 修改入口组显隐状态。
     */
    getAvatarSummonMode(): AvatarSummonMode {
      return state.summonMode;
    },

    /**
     * hoverEnter 处理角色悬停进入。
     * 控制器本身不直接改窗口显示策略，而是交给 reducer 推导后统一同步，保证状态机仍是单一事实来源。
     */
    hoverEnter() {
      state = reduceShellState(state, { type: "avatar.hover_enter" });
      syncShell();
    },

    /**
     * hoverLeave 处理角色悬停离开。
     * 是否真的回到完全静默态由 reducer 决定，控制器只负责执行结果，避免把面板开关规则散落在这里。
     */
    hoverLeave() {
      state = reduceShellState(state, { type: "avatar.hover_leave" });
      syncShell();
    },

    /**
     * toggleAvatarSummonedVisibility 处理用户通过快捷键或托盘显式召回/收起桌宠。
     * 这是长期显示意图，不再和 hover 派生的临时交互态混在一起。
     */
    toggleAvatarSummonedVisibility(): AvatarSummonMode {
      state = reduceShellState(state, { type: "avatar.visibility_toggle_requested" });
      if (state.summonMode !== "hidden") {
        input.avatarWindow.show();
      }
      syncShell();
      return state.summonMode;
    },

    /**
     * startDrag 处理角色窗拖拽开始。
     * 控制器只推进壳层状态并同步穿透策略，不计算窗口坐标，保持窗口移动职责留在 bootstrap 接线层。
     */
    startDrag() {
      state = reduceShellState(state, { type: "avatar.drag_started" });
      syncShell();
    },

    /**
     * endDrag 处理角色窗拖拽结束。
     * reducer 会根据面板是否开启决定回到悬停态还是面板锚点，控制器只执行最终投影。
     */
    endDrag() {
      state = reduceShellState(state, { type: "avatar.drag_ended" });
      syncShell();
    },

    /**
     * startMaterialDrop 在系统文件拖进头像窗口时临时收回鼠标穿透。
     * 这只服务于让 renderer 稳定收到 drag/drop 事件，不承担材料提交业务。
     */
    startMaterialDrop() {
      state = reduceShellState(state, { type: "avatar.material_drop_enter" });
      syncShell();
    },

    /**
     * endMaterialDrop 在投递完成、取消或离开窗口后恢复被动穿透。
     */
    endMaterialDrop() {
      state = reduceShellState(state, { type: "avatar.material_drop_leave" });
      syncShell();
    },

    /**
     * openPanel 负责串起“点击入口 -> 计算停靠位置 -> 打开并聚焦面板 -> 状态切到 focused”这条完整链路。
     * preferredSide 先沿用当前状态中的偏好方向，后续如果入口事件带入显式方向，也只需要替换这一处调用参数。
     */
    openPanel(view: PanelView) {
      state = reduceShellState(state, {
        type: "avatar.open_panel_clicked",
        payload: { preferredSide: state.preferredPanelSide, view }
      });
      syncShell();

      // 视图、尺寸和停靠位置必须作为同一打开事务更新，避免面板先闪出旧内容或旧尺寸。
      const panelSize = PANEL_VIEW_SIZES.models;
      const placement = getPanelPlacement(resolvePanelPlacementInput(), panelSize);
      input.panelWindow.openAt(placement, panelSize);
      input.panelWindow.focus();

      // 面板请求聚焦之后，控制器需要把纯状态推进到 focused_open，
      // 这样后续窗口同步语义才能与设计文档中的 opening -> focused_open 生命周期一致。
      state = reduceShellState(state, { type: "panel.focused" });
      syncShell();
    },

    /**
     * closePanelFromUserRequest 处理侧边板关闭按钮或显式 panel:close。
     * 这条路径代表用户主动关闭，不应和外部点击自动关闭共用同一个入口。
     */
    closePanelFromUserRequest() {
      state = reduceShellState(state, { type: "panel.user_close_requested" });
      syncShell();
    },

    /**
     * closePanelFromExternalBlur 处理真实外部点击导致的自动关闭。
     * 控制器只关心最终状态收口，不区分是谁触发了外部关闭守卫。
     */
    closePanelFromExternalBlur() {
      state = reduceShellState(state, { type: "panel.external_click_close" });
      syncShell();
    },

    /**
     * refreshPanelPlacement 负责在关键节点重新计算侧边窗位置。
     * 主进程只在人物尺寸变化结束后调用它，避免侧边窗跟着每次细碎拖拽抖动。
     */
    refreshPanelPlacement() {
      if (state.panelMode === "closed") {
        return;
      }

      const panelSize = PANEL_VIEW_SIZES.models;
      const placement = getPanelPlacement(resolvePanelPlacementInput(), panelSize);
      input.panelWindow.openAt(placement, panelSize);
    }
  };
}
