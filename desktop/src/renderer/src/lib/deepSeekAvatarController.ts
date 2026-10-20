import type { CompanionDisplayState } from "../companion/companionStatus";
import type { Live2dLipSyncModelBinding } from "./live2dLipSync";

type Reaction = "Hammer" | "BubbleGum" | "SprayWater" | "OpenCase" | "Selfie" | "SelfieQuick" | "Ketchup";
type ParameterValues = Readonly<Record<string, number>>;

// 只有吹泡泡会占用独立嘴部美术；说话时跳过它，其他动作仍由原有口型控制器驱动嘴巴。
const REACTIONS: readonly Reaction[] = ["Hammer", "Selfie", "SelfieQuick", "Ketchup", "BubbleGum", "SprayWater", "OpenCase"];
const STATE_PARAMETERS: Partial<Record<CompanionDisplayState, ParameterValues>> = {
  listening: { ParamCheek16: 1 },
  thinking: { ParamCheek74: 1 },
  waiting_user: { ParamCheek74: 1 },
  responding: { ParamCheek76: 1, ParamMouthForm: 1 },
  working: { pointZ: 1 }
};

/** 控制器只依赖动作和参数接口，保持与 Pixi 画布及 Web Audio 生命周期分离。 */
type ReactionModel = {
  destroyed?: boolean;
  autoFocus: boolean;
  autoHitTest: boolean;
  motion(group: string, index: number, priority: number, options: { loop: boolean }): Promise<boolean>;
  internalModel: {
    getIdSafe(id: string): unknown;
    on(event: "beforeModelUpdate", listener: () => void): unknown;
    off(event: "beforeModelUpdate", listener: () => void): unknown;
    coreModel: {
      getModel(): { parameters: { values: Float32Array; defaultValues: Float32Array } };
      saveParameters(): void;
      setParameterValueById(id: unknown, value: number, weight: number): void;
    };
    motionManager: {
      loadMotion(group: string, index: number): Promise<unknown>;
      stopAllMotions(): void;
      on(event: "motionFinish", listener: () => void): unknown;
      off(event: "motionFinish", listener: () => void): unknown;
    };
  };
};

type BoundModel = {
  model: ReactionModel;
  baseline: Float32Array;
  autoFocus: boolean;
  autoHitTest: boolean;
};

type ActiveReaction = {
  generation: number;
  requested: Reaction;
  runningStep: Reaction | null;
};

type ControllerOptions = {
  reportDiagnostic?(level: "warn", message: string, details?: unknown): void;
};

/**
 * 管理大肥鱼的状态表情和一次性动作，不参与语音合成、聊天或窗口移动。
 *
 * @param options 可选诊断出口；动作失败时保留模型并恢复待机。
 */
export class DeepSeekAvatarController {
  private binding: BoundModel | null = null;
  private state: CompanionDisplayState = "syncing";
  private generation = 0;
  private reaction: ActiveReaction | null = null;
  private nextReactionIndex = 0;
  private pendingStart: Promise<void> = Promise.resolve();

  constructor(private readonly options: ControllerOptions = {}) {}

  /**
   * 绑定舞台上已提交的模型；其他模型继续使用原来的行为。
   *
   * @param binding 实际模型、ticker 和资源路径；null 表示模型即将离开舞台。
   * @returns 当前模型是否支持大肥鱼专属动作。
   */
  setModelBinding(binding: Live2dLipSyncModelBinding | null): boolean {
    this.releaseBinding();
    if (!binding?.modelPath?.replaceAll("\\", "/").match(/\/DeepSeekChibi\/c_0120\.model3\.json(?:[?#]|$)/)) {
      return false;
    }

    const model = binding.model as unknown as ReactionModel;
    const internal = model.internalModel;
    if (typeof model.motion !== "function" || typeof internal?.coreModel?.getModel !== "function"
      || typeof internal?.motionManager?.loadMotion !== "function") {
      this.options.reportDiagnostic?.("warn", "avatar.reaction_model_unsupported");
      return false;
    }

    const parameters = internal.coreModel.getModel().parameters;
    this.binding = {
      model,
      baseline: new Float32Array(parameters.defaultValues),
      autoFocus: model.autoFocus,
      autoHitTest: model.autoHitTest
    };
    // 鼠标跟随和引擎自动点按会与 NearMate 的拖动、聊天入口及动作状态争用同一个模型。
    model.autoFocus = false;
    model.autoHitTest = false;
    internal.on("beforeModelUpdate", this.applyPresentation);
    internal.motionManager.on("motionFinish", this.handleMotionFinished);
    this.restorePose();
    return true;
  }

  /**
   * 跟随已有的当前角色状态更新表情，不另行推断业务状态。
   *
   * @param state Chat 与 Avatar 共用的当前角色状态。
   * @returns 无返回值；状态变化会取消尚未完成的逗趣动作。
   */
  setState(state: CompanionDisplayState): void {
    if (this.state === state) return;
    this.state = state;
    this.cancelReaction();
  }

  /**
   * 轻点角色时给出敲锤子的反馈；打开聊天仍由调用方负责。
   *
   * @returns 无返回值；没有绑定大肥鱼时不产生副作用。
   */
  reactToTap(): void {
    this.requestReaction("Hammer");
  }

  /**
   * 按顺序播放下一个逗趣动作，连续点击只保留最后一次请求。
   *
   * @returns 无返回值；说话时自动跳过会遮住嘴巴的吹泡泡。
   */
  playNextReaction(): void {
    if (!this.binding) return;
    let reaction = REACTIONS[this.nextReactionIndex++ % REACTIONS.length];
    if (reaction === "BubbleGum" && this.state === "responding") {
      reaction = REACTIONS[this.nextReactionIndex++ % REACTIONS.length];
    }
    this.requestReaction(reaction);
  }

  /**
   * 取消异步动作并移除模型事件订阅。
   *
   * @returns 无返回值；可重复调用，迟到的加载结果不会再操作模型。
   */
  destroy(): void {
    this.releaseBinding();
  }

  private requestReaction(requested: Reaction): void {
    if (!this.binding) return;
    this.cancelReaction();
    this.reaction = { generation: this.generation, requested, runningStep: null };
    void this.startStep(requested === "Selfie" ? "OpenCase" : requested, this.reaction);
  }

  private async startStep(group: Reaction, reaction: ActiveReaction): Promise<void> {
    const binding = this.binding;
    if (!binding) return;
    try {
      const motion = await binding.model.internalModel.motionManager.loadMotion(group, 0);
      if (!this.isCurrent(binding, reaction)) return;
      if (!motion) throw new Error(`无法加载动作：${group}`);

      // 启动本身也是异步操作。串行完成它，防止旧请求的迟到清理误停新动作。
      const task = this.pendingStart.then(async () => {
        if (!this.isCurrent(binding, reaction)) return;
        const started = await binding.model.motion(group, 0, 3, { loop: false });
        if (!this.isCurrent(binding, reaction)) {
          if (this.binding === binding && !binding.model.destroyed) this.restorePose();
          return;
        }
        if (!started) throw new Error(`动作未能启动：${group}`);
        reaction.runningStep = group;
      });
      this.pendingStart = task.catch(() => undefined);
      await task;
    } catch (error) {
      if (!this.isCurrent(binding, reaction)) return;
      this.options.reportDiagnostic?.("warn", "avatar.reaction_failed", { group, error });
      this.cancelReaction();
    }
  }

  private isCurrent(binding: BoundModel, reaction: ActiveReaction): boolean {
    return this.binding === binding && !binding.model.destroyed
      && this.reaction === reaction && this.generation === reaction.generation;
  }

  private readonly handleMotionFinished = (): void => {
    const binding = this.binding;
    const reaction = this.reaction;
    if (!binding || !reaction?.runningStep || !this.isCurrent(binding, reaction)) return;
    const finishedStep = reaction.runningStep;
    reaction.runningStep = null;

    // 引擎会在帧内发完成事件；等它完成状态收尾后再切下一动作或恢复参数。
    queueMicrotask(() => {
      if (!this.isCurrent(binding, reaction)) return;
      if (reaction.requested === "Selfie" && finishedStep === "OpenCase") {
        void this.startStep("Selfie", reaction);
        return;
      }
      this.cancelReaction();
    });
  };

  private readonly applyPresentation = (): void => {
    const binding = this.binding;
    if (!binding || binding.model.destroyed) return;
    const values = this.reaction
      ? this.reaction.requested === "SprayWater" ? { jingyu: 1 } : {}
      : STATE_PARAMETERS[this.state] ?? {};
    const internal = binding.model.internalModel;
    for (const [id, value] of Object.entries(values)) {
      // 临时表情写在最终渲染前，不累计 Add 偏移，也不碰语音使用的 MouthOpenY。
      internal.coreModel.setParameterValueById(internal.getIdSafe(id), value, 1);
    }
  };

  private cancelReaction(): void {
    this.generation += 1;
    this.reaction = null;
    this.restorePose();
  }

  private restorePose(): void {
    const binding = this.binding;
    if (!binding || binding.model.destroyed) return;
    const internal = binding.model.internalModel;
    internal.motionManager.stopAllMotions();
    internal.coreModel.getModel().parameters.values.set(binding.baseline);
    internal.coreModel.saveParameters();
    // 下一帧由引擎正常请求 Idle；不保留手机、锤子或番茄酱的最后一帧。
  }

  private releaseBinding(): void {
    this.generation += 1;
    this.reaction = null;
    this.pendingStart = Promise.resolve();
    const binding = this.binding;
    if (!binding) return;
    const internal = binding.model.internalModel;
    internal.off("beforeModelUpdate", this.applyPresentation);
    internal.motionManager.off("motionFinish", this.handleMotionFinished);
    this.restorePose();
    if (!binding.model.destroyed) {
      binding.model.autoFocus = binding.autoFocus;
      binding.model.autoHitTest = binding.autoHitTest;
    }
    this.binding = null;
  }
}
