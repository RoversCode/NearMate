/**
 * 播放 NearMate 的短按键音，并在卸载时释放 Web Audio 资源。
 * 每次按键重新触发 90ms 的轻提示，避免连按堆成一串迟到音效。
 */
export class ShortcutSound {
  private context: AudioContext | null = null;
  private oscillator: OscillatorNode | null = null;
  private generation = 0;

  /**
   * 播放一次双音高的短反馈；失败交由调用方记录，不影响业务快捷键。
   * @returns 音效完成启动后的 Promise。
   */
  async play(): Promise<void> {
    const generation = ++this.generation;
    const context = this.context ??= new AudioContext();
    if (context.state === "suspended") await context.resume();
    if (generation !== this.generation || this.context !== context) return;
    this.oscillator?.stop();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const now = context.currentTime;
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(740, now);
    oscillator.frequency.setValueAtTime(988, now + 0.035);
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(0.07, now + 0.006);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.09);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.onended = () => {
      oscillator.disconnect();
      gain.disconnect();
      if (this.oscillator === oscillator) this.oscillator = null;
    };
    this.oscillator = oscillator;
    oscillator.start(now);
    oscillator.stop(now + 0.095);
  }

  /**
   * 停止反馈并回收音频上下文。
   * @returns 无返回值。
   */
  dispose(): void {
    this.generation += 1;
    this.oscillator?.stop();
    this.oscillator = null;
    const context = this.context;
    this.context = null;
    if (context) void context.close().catch(() => undefined);
  }
}
