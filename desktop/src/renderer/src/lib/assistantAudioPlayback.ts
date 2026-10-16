/**
 * 描述 renderer 播放助手远端音轨所需的最小 Track 能力。
 *
 * 功能：隔离 LiveKit SDK 具体类型，只让播放边界依赖 attach/detach 两个媒体资源方法。
 * 核心入参：由 LiveKit Room 的 TrackSubscribed 事件传入真实远端音轨。
 * 预期输出：AssistantAudioPlayback 可以把音轨绑定到受控 HTMLAudioElement。
 * 边界异常：真实 SDK 类型变化时，只要仍保留 attach 能力，播放模块不需要跟随大范围修改。
 */
export type AttachableAssistantAudioTrack = {
  attach(): HTMLMediaElement;
  detach?(): HTMLMediaElement[];
};

/**
 * 助手音频播放 sink 协议。
 *
 * 功能：让 LiveKitRoomClient 只负责识别远端助手音轨，不直接拥有 DOM 播放细节。
 * 核心入参：track 是 LiveKit 远端音轨对象。
 * 预期输出：playTrack 完成音频元素 attach/play；release 释放当前播放资源。
 * 边界异常：playTrack 可以因浏览器自动播放策略失败而 reject，调用方负责诊断展示。
 */
export type AssistantAudioPlaybackSink = {
  playTrack(track: unknown): Promise<void>;
  release(): void;
};

export type AssistantAudioPlaybackOptions = {
  onPlaybackElementChanged?(element: HTMLMediaElement | null): void;
};

/**
 * renderer 内的助手音频播放边界。
 *
 * 功能：把 LiveKit 远端助手音轨 attach 到隐藏 audio 元素并启动播放，同时管理旧音轨释放。
 * 核心入参：documentRef 用于创建/挂载 audio 元素，测试可注入 jsdom document。
 * 预期输出：桌面端真正听到 backend 发布的 assistant-audio 轨道。
 * 边界异常：track 缺少 attach、audio.play 被策略拒绝或 DOM 不可用时抛出清晰错误。
 */
export class AssistantAudioPlayback implements AssistantAudioPlaybackSink {
  private readonly documentRef: Document;
  private readonly onPlaybackElementChanged?: AssistantAudioPlaybackOptions["onPlaybackElementChanged"];
  private currentTrack: AttachableAssistantAudioTrack | null = null;
  private currentElement: HTMLMediaElement | null = null;
  private muted = false;
  private playbackGeneration = 0;

  /**
   * 初始化播放边界。
   *
   * 核心入参：documentRef 默认使用全局 document，便于生产环境创建 audio 元素。
   * 预期输出：得到可复用的助手音频播放 sink。
   * 边界异常：构造阶段不触碰 DOM，避免应用启动时无音频轨也创建元素。
   */
  constructor(
    documentRef: Document = document,
    options: AssistantAudioPlaybackOptions = {}
  ) {
    this.documentRef = documentRef;
    this.onPlaybackElementChanged = options.onPlaybackElementChanged;
  }

  /**
   * 播放一条新的助手远端音轨。
   *
   * @param track LiveKit TrackSubscribed 事件传入的远端音轨。
   * @returns 新音轨继承静音门禁并完成 play 后的 Promise；旧尝试不会再修改播放资源。
   * @throws 音轨缺少 attach 或播放失败时抛出错误。
   */
  async playTrack(track: unknown): Promise<void> {
    if (!isAttachableAssistantAudioTrack(track)) {
      throw new Error("助手音频轨缺少 attach 方法");
    }

    // 新音轨到达时先释放旧元素，防止多个远端音轨重叠播放。
    this.release();
    const playbackGeneration = this.playbackGeneration;
    const attachedElement = track.attach();
    this.currentTrack = track;
    this.currentElement = attachedElement;

    // LiveKit attach 返回的通常是 audio 元素；这里统一设置隐藏自动播放属性，避免污染 UI 布局。
    attachedElement.autoplay = true;
    attachedElement.muted = this.muted;
    attachedElement.setAttribute("data-companion-assistant-audio", "true");
    attachedElement.style.display = "none";
    this.documentRef.body.appendChild(attachedElement);

    try {
      const playResult = attachedElement.play();
      if (playResult instanceof Promise) {
        await playResult;
      }
      // LiveKit 会复用已释放的 audio 元素，只能用播放代际判断迟到结果是否仍属于当前音轨。
      if (this.playbackGeneration === playbackGeneration) {
        this.onPlaybackElementChanged?.(this.muted ? null : attachedElement);
      }
    } catch (error) {
      if (this.playbackGeneration === playbackGeneration) this.release();
      throw error;
    }
  }

  /**
   * 立即静音当前音轨，并让重连或迟到音轨继承同一播放门禁。
   * @param muted 是否停止发声；明确的新交付才允许解除。
   * @returns 无返回值，立即更新媒体元素和唇形分析入口。
   */
  setMuted(muted: boolean): void {
    this.muted = muted;
    if (!this.currentElement) return;
    this.currentElement.muted = muted;
    this.onPlaybackElementChanged?.(muted ? null : this.currentElement);
  }

  /**
   * 释放当前助手音频播放资源。
   *
   * 功能：从 DOM 移除当前 audio 元素，并通知 LiveKit track detach。
   * 核心入参：无，读取当前播放状态。
   * 预期输出：当前远端音轨不再由 renderer 播放。
   * 边界异常：detach 或 DOM 移除失败不应阻塞后续重连，浏览器会自然回收已移除元素。
   * @returns 无返回值，同时让在途旧 play 的成功和失败结果失效。
   */
  release(): void {
    this.playbackGeneration += 1;
    const element = this.currentElement;
    const track = this.currentTrack;
    this.currentElement = null;
    this.currentTrack = null;
    // DOM 移除不保证媒体立即停声；先静音，detach 再释放 SDK 的绑定。
    if (element) element.muted = true;

    if (element || track) {
      this.onPlaybackElementChanged?.(null);
    }

    // detach 是 LiveKit 释放媒体绑定的正式入口；老测试替身缺少该方法时跳过即可。
    if (track?.detach) {
      track.detach();
    }

    if (element?.parentNode) {
      element.parentNode.removeChild(element);
    }
  }
}

/**
 * 判断未知对象是否具备可 attach 的远端音轨能力。
 *
 * 核心入参：candidate 来自 LiveKit TrackSubscribed 事件。
 * 预期输出：具备 attach 函数时返回 true。
 * 边界异常：只做结构判断，不读取 SDK 私有字段。
 */
function isAttachableAssistantAudioTrack(candidate: unknown): candidate is AttachableAssistantAudioTrack {
  return typeof candidate === "object" && candidate !== null && typeof Reflect.get(candidate, "attach") === "function";
}
