import { createAudioAnalyser, type RemoteAudioTrack } from "livekit-client";

const DEFAULT_ATTACK_THRESHOLD = 0.035;
const DEFAULT_RELEASE_DELAY_MS = 240;

type AudioAnalyser = {
  calculateVolume(): number;
  cleanup(): Promise<void> | void;
};

type ListeningMonitorConfig = {
  attackThreshold?: number;
  releaseDelayMs?: number;
  createAnalyser?(track: unknown): AudioAnalyser;
  requestFrame?(callback: FrameRequestCallback): number;
  cancelFrame?(frameId: number): void;
};

/**
 * LocalListeningMonitor 描述 clean-mic 音量监测的生命周期能力。
 */
export type LocalListeningMonitor = {
  start(track: unknown, onActiveChanged: (active: boolean) => void): void;
  stop(track?: unknown): void;
};

/**
 * LiveKitLocalListeningMonitor 把 clean-mic 的真实音量转换成稳定的本地聆听提示。
 */
export class LiveKitLocalListeningMonitor implements LocalListeningMonitor {
  private readonly attackThreshold: number;
  private readonly releaseDelayMs: number;
  private readonly createAnalyser: (track: unknown) => AudioAnalyser;
  private readonly requestFrame: (callback: FrameRequestCallback) => number;
  private readonly cancelFrame: (frameId: number) => void;
  private analyser: AudioAnalyser | null = null;
  private currentTrack: unknown = null;
  private frameId: number | null = null;
  private lastVoiceAt = 0;
  private active = false;
  private onActiveChanged: ((active: boolean) => void) | null = null;

  /**
   * 初始化本地聆听监测器。
   *
   * Args:
   *   config: 测试替身、音量阈值和释放延迟；生产环境使用 LiveKit 默认分析器。
   */
  constructor(config: ListeningMonitorConfig = {}) {
    this.attackThreshold = config.attackThreshold ?? DEFAULT_ATTACK_THRESHOLD;
    this.releaseDelayMs = config.releaseDelayMs ?? DEFAULT_RELEASE_DELAY_MS;
    this.createAnalyser = config.createAnalyser ?? (track =>
      createAudioAnalyser(track as RemoteAudioTrack)
    );
    this.requestFrame = config.requestFrame ?? (callback =>
      globalThis.requestAnimationFrame(callback)
    );
    this.cancelFrame = config.cancelFrame ?? (frameId =>
      globalThis.cancelAnimationFrame(frameId)
    );
  }

  /**
   * start 开始分析一条新的 clean-mic 轨道。
   *
   * Args:
   *   track: LiveKit 下发的 clean-mic 远端音轨。
   *   onActiveChanged: 只在稳定状态变化时接收 true 或 false。
   */
  start(track: unknown, onActiveChanged: (active: boolean) => void): void {
    this.stop();

    try {
      this.analyser = this.createAnalyser(track);
    } catch {
      return;
    }

    this.currentTrack = track;
    this.onActiveChanged = onActiveChanged;
    this.frameId = this.requestFrame(timestamp => this.sample(timestamp));
  }

  /**
   * stop 释放当前音量分析资源。
   *
   * Args:
   *   track: 可选的退订轨道；传入旧轨道时不会误停后来接管的新轨道。
   */
  stop(track?: unknown): void {
    if (track !== undefined && track !== this.currentTrack) {
      return;
    }

    if (this.frameId !== null) {
      this.cancelFrame(this.frameId);
      this.frameId = null;
    }

    const analyser = this.analyser;
    this.analyser = null;
    this.currentTrack = null;
    this.lastVoiceAt = 0;

    if (analyser) {
      try {
        void Promise.resolve(analyser.cleanup()).catch(() => undefined);
      } catch {
        // 音频上下文清理失败不能阻断房间断开或窗口卸载。
      }
    }

    const activeChanged = this.active;
    this.active = false;
    const listener = this.onActiveChanged;
    this.onActiveChanged = null;
    if (activeChanged) {
      listener?.(false);
    }
  }

  /**
   * sample 读取一帧音量并推进攻击或释放状态。
   *
   * Args:
   *   timestamp: 当前动画帧时间戳。
   */
  private sample(timestamp: number): void {
    if (!this.analyser) {
      return;
    }

    const volume = this.analyser.calculateVolume();
    if (Number.isFinite(volume) && volume >= this.attackThreshold) {
      this.lastVoiceAt = timestamp;
      this.setActive(true);
    } else if (this.active && timestamp - this.lastVoiceAt >= this.releaseDelayMs) {
      this.setActive(false);
    }

    this.frameId = this.requestFrame(nextTimestamp => this.sample(nextTimestamp));
  }

  /**
   * setActive 只在布尔状态真的变化时通知 Avatar runtime。
   *
   * Args:
   *   active: 当前是否检测到用户正在说话。
   */
  private setActive(active: boolean): void {
    if (this.active === active) {
      return;
    }

    this.active = active;
    this.onActiveChanged?.(active);
  }
}
