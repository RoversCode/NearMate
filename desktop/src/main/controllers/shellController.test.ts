import { describe, expect, it, vi } from "vitest";
import { createShellController } from "./shellController";

/**
 * createFakeWindows 统一构造控制器测试使用的假窗口端口。
 * 入参允许按需覆盖角色窗边界，输出则返回两组带 spy 的窗口端口，供每个用例直接断言主进程编排结果。
 * 边界：这里只复用重复样板，不隐藏任何关键行为，避免测试可读性被无意义封装削弱。
 */
function createFakeWindows(input?: {
  avatarBounds?: { x: number; y: number; width: number; height: number };
  petBounds?: { x: number; y: number; width: number; height: number } | null;
}) {
  const avatarBounds = input?.avatarBounds ?? { x: 100, y: 120, width: 180, height: 320 };
  const petBounds = input?.petBounds ?? null;

  /**
   * avatarWindow 模拟角色窗最小端口能力，
   * 用于观察控制器如何把 reducer 状态投影成显示与鼠标穿透指令。
   */
  const avatarWindow = {
    show: vi.fn(),
    hide: vi.fn(),
    applyPresentationMode: vi.fn(),
    getBounds: vi.fn(() => avatarBounds),
    getPetBounds: vi.fn(() => petBounds)
  };

  /**
   * panelWindow 模拟独立面板窗端口，
   * 用于验证控制器只下发打开、聚焦、隐藏命令，而不越界承担真实窗口创建职责。
   */
  const panelWindow = {
    hide: vi.fn(),
    openAt: vi.fn(),
    focus: vi.fn()
  };

  return { avatarWindow, panelWindow };
}

describe("createShellController", () => {
  it("启动时只显示角色窗，面板窗保持隐藏", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();

    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.start();

    expect(avatarWindow.show).toHaveBeenCalledOnce();
    expect(panelWindow.hide).toHaveBeenCalledOnce();
    expect(avatarWindow.applyPresentationMode).toHaveBeenCalledWith("passive");
  });

  it("启动可见态按快捷键会隐藏角色窗并同步隐藏面板", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    const summonMode = controller.toggleAvatarSummonedVisibility();

    expect(summonMode).toBe("hidden");
    expect(avatarWindow.show).not.toHaveBeenCalled();
    expect(avatarWindow.applyPresentationMode).toHaveBeenCalledWith("hidden");
    expect(panelWindow.hide).toHaveBeenCalled();
  });

  it("召回置顶后 hover leave 不会把角色窗降回被动展示", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.toggleAvatarSummonedVisibility();
    controller.toggleAvatarSummonedVisibility();
    avatarWindow.applyPresentationMode.mockClear();
    controller.hoverLeave();

    expect(avatarWindow.applyPresentationMode).toHaveBeenCalledWith("summoned");
  });

  it("隐藏态再次按快捷键会把角色窗切到召回置顶展示", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.toggleAvatarSummonedVisibility();
    const summonMode = controller.toggleAvatarSummonedVisibility();

    expect(summonMode).toBe("summoned_topmost");
    expect(avatarWindow.show).toHaveBeenCalledOnce();
    expect(avatarWindow.applyPresentationMode).toHaveBeenLastCalledWith("summoned");
  });

  it("隐藏态收到 hover enter 不会错误切到置顶交互", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.toggleAvatarSummonedVisibility();
    avatarWindow.applyPresentationMode.mockClear();
    controller.hoverEnter();

    expect(avatarWindow.applyPresentationMode).toHaveBeenCalledWith("hidden");
  });

  it("点击面板入口时，按定位结果打开独立面板窗", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();

    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.openPanel("models");

    /**
     * 面板被打开后必须继续请求聚焦，
     * 这样控制器才算完整走通了从 opening 到 focused_open 的主进程编排链路。
     */
    expect(panelWindow.openAt).toHaveBeenCalledWith(
      { x: 296, y: 120, side: "right" },
      { width: 380, height: 560 }
    );
    expect(panelWindow.focus).toHaveBeenCalledOnce();

    /**
     * 面板打开并聚焦后，角色窗必须回到被动穿透。
     * 侧边板本身是独立窗口，不能被头像透明矩形抢走点击。
     */
    expect(avatarWindow.applyPresentationMode).toHaveBeenLastCalledWith("passive");
  });

  it("打开面板前会先把头像窗口退回被动穿透", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();

    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.hoverEnter();
    avatarWindow.applyPresentationMode.mockClear();

    controller.openPanel("models");

    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(1, "passive");
    expect(avatarWindow.applyPresentationMode).toHaveBeenLastCalledWith("passive");
  });

  it("未提供定位依赖时，不应静默猜测显示器边界", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();

    /**
     * 这里故意绕过静态类型约束，模拟未来接线层若以动态对象调用控制器时的运行时风险。
     * 业务要求是不允许控制器私自猜测显示器尺寸，因此必须显式抛错而不是偷偷回退到硬编码默认值。
     */
    const createControllerWithoutPlacement = () =>
      createShellController({
        avatarWindow,
        panelWindow
      } as never);

    expect(createControllerWithoutPlacement).toThrowError(
      "Shell controller requires either getPanelPlacement or getDisplayBounds."
    );
  });

  it("使用真实面板定位服务接线时，会根据显示器边界计算 placement", () => {
    const { avatarWindow, panelWindow } = createFakeWindows({
      avatarBounds: {
        x: 1180,
        y: 200,
        width: 180,
        height: 320
      }
    });

    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getDisplayBounds: () => ({ x: 0, y: 0, width: 1440, height: 900 })
    });

    controller.openPanel("models");

    /**
     * 右侧空间不足时，控制器应当通过真实 placement service 把面板翻转到左侧，
     * 这样这个测试才能覆盖“controller 正确接线纯定位服务”这条关键质量边界。
     */
    expect(panelWindow.openAt).toHaveBeenCalledWith(
      { x: 784, y: 80, side: "left" },
      { width: 380, height: 560 }
    );
  });

  it("hover enter 与 hover leave 会在关闭态下切换角色窗穿透", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.hoverEnter();
    controller.hoverLeave();

    /**
     * hover 进入时角色窗要进入交互模式承接操作，离开且面板关闭后再恢复被动展示，
     * 这是 controller 最核心的状态投影行为之一，因此直接断言调用序列最稳妥。
     */
    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(1, "interactive");
    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(2, "passive");
  });

  it("拖拽开始与结束会收回穿透并回到悬停交互态", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.startDrag();
    controller.endDrag();

    /**
     * 拖拽期间角色窗必须进入交互模式，否则 renderer 收不到后续 pointermove。
     * 拖拽结束后停留在 hover_revealed，继续保持交互展示，避免鼠标仍在人物上却立刻穿透到桌面。
     */
    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(1, "interactive");
    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(2, "interactive");
  });

  it("材料投递进入与结束会临时收回穿透并回到被动展示", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.startMaterialDrop();
    controller.endMaterialDrop();

    /**
     * 文件从系统拖进来时，头像窗口必须临时接收 drag/drop 事件。
     * 投递完成或离开后要恢复穿透，避免透明窗口长期挡住桌面。
     */
    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(1, "interactive");
    expect(avatarWindow.applyPresentationMode).toHaveBeenNthCalledWith(2, "passive");
  });

  it("面板打开期间再次命中人物时，只收回穿透但不置顶遮挡侧边板", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.openPanel("models");
    avatarWindow.applyPresentationMode.mockClear();

    controller.hoverEnter();

    /**
     * panel-adjacent-interactive 会让人物本体仍能拖拽/缩放，
     * 但不会把整个头像透明窗口浮到侧边板之上。
     */
    expect(avatarWindow.applyPresentationMode).toHaveBeenCalledWith(
      "panel-adjacent-interactive"
    );
  });

  it("用户主动关闭面板后，会隐藏面板并恢复角色窗穿透", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.openPanel("models");
    controller.closePanelFromUserRequest();

    /**
     * 用户主动关闭是 panel 生命周期的关键收口场景，
     * 控制器必须同时把面板窗隐藏掉，并把角色窗重新同步回完全 idle 的被动展示状态。
     */
    expect(panelWindow.hide).toHaveBeenCalledOnce();
    expect(avatarWindow.applyPresentationMode).toHaveBeenLastCalledWith("passive");
  });

  it("外部点击关闭面板后，也会隐藏面板并恢复角色窗穿透", () => {
    const { avatarWindow, panelWindow } = createFakeWindows();
    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement: () => ({ x: 296, y: 120, side: "right" })
    });

    controller.openPanel("models");
    controller.closePanelFromExternalBlur();

    expect(panelWindow.hide).toHaveBeenCalledOnce();
    expect(avatarWindow.applyPresentationMode).toHaveBeenLastCalledWith("passive");
  });

  it("打开面板时优先使用人物锚点而不是透明窗口外框", () => {
    const getPanelPlacement = vi.fn(() => ({ x: 321, y: 0, side: "right" as const }));
    const { avatarWindow, panelWindow } = createFakeWindows({
      avatarBounds: { x: 0, y: 0, width: 640, height: 760 },
      petBounds: { x: 142, y: 36, width: 163, height: 479 }
    });

    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement
    });

    controller.openPanel("models");

    /**
     * 人物边界会先被收敛成靠右侧身体的锚点区域。
     * 如果这里退回透明窗口外框，x 会接近 0/640，而不是下面这个人物锚点。
     */
    expect(getPanelPlacement).toHaveBeenCalledWith(
      { x: 210, y: 65, width: 95, height: 422 },
      { width: 380, height: 560 }
    );
    expect(panelWindow.openAt).toHaveBeenCalledWith(
      { x: 321, y: 0, side: "right" },
      { width: 380, height: 560 }
    );
  });

  it("面板打开后，拖拽结束会按最新人物锚点重新定位一次", () => {
    const getPanelPlacement = vi
      .fn()
      .mockReturnValueOnce({ x: 321, y: 0, side: "right" as const })
      .mockReturnValueOnce({ x: 520, y: 40, side: "right" as const });
    const { avatarWindow, panelWindow } = createFakeWindows({
      petBounds: { x: 142, y: 36, width: 163, height: 479 }
    });

    const controller = createShellController({
      avatarWindow,
      panelWindow,
      getPanelPlacement
    });

    controller.openPanel("models");
    controller.startDrag();
    controller.endDrag();
    controller.refreshPanelPlacement();

    expect(panelWindow.openAt).toHaveBeenNthCalledWith(
      1,
      { x: 321, y: 0, side: "right" },
      { width: 380, height: 560 }
    );
    expect(panelWindow.openAt).toHaveBeenNthCalledWith(
      2,
      { x: 520, y: 40, side: "right" },
      { width: 380, height: 560 }
    );
  });
});
