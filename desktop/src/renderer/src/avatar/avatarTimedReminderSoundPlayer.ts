import alarmSoundUrl from "../../../../../assets/audio/闹钟.mp3?url";
import vibrationSoundUrl from "../../../../../assets/audio/震动.mp3?url";

type ReminderAudio = Pick<HTMLAudioElement,
  "play" | "pause" | "currentTime" | "loop" | "onended" | "onerror"
>;

/**
 * 定义一轮短音效的开始与停止操作。
 *
 * Args:
 *   onFinished: 两段声音结束或播放失败后的回调；主动停止时不调用。
 */
export type TimedReminderSoundPlayer = {
  play(onFinished: () => void): void;
  stop(): void;
};

/**
 * 串行播放一次闹钟音和一次震动音，停止后丢弃旧音频的迟到事件。
 *
 * Args:
 *   options: 音频工厂和失败诊断出口；默认使用浏览器音频元素。
 *
 * Returns:
 *   不接入角色语音或唇形分析的本地播放器。
 */
export function createTimedReminderSoundPlayer(options: {
  createAudio?(source: string): ReminderAudio;
  reportPlaybackError(error: unknown): void;
}): TimedReminderSoundPlayer {
  let currentAudio: ReminderAudio | null = null;
  let generation = 0;
  let timeout: ReturnType<typeof setTimeout> | null = null;

  /** 解绑事件后再暂停，避免暂停过程的迟到事件开启下一段声音。 */
  function stop(): void {
    generation += 1;
    if (timeout !== null) clearTimeout(timeout);
    timeout = null;
    const audio = currentAudio;
    currentAudio = null;
    if (!audio) return;
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.currentTime = 0;
  }

  return {
    stop,
    play(onFinished): void {
      stop();
      const playbackGeneration = generation;

      /**
       * 播放下一段素材，只有当前音频正常结束才能继续。
       *
       * Args:
       *   index: 闹钟音为零，震动音为一。
       */
      function playSegment(index: number): void {
        if (playbackGeneration !== generation) return;
        if (index === 2) {
          stop();
          onFinished();
          return;
        }
        let audio: ReminderAudio | null = null;

        /** 当前素材播放失败时结束本轮，旧素材的失败回调不影响替代播放。 */
        function fail(error: unknown): void {
          if (playbackGeneration !== generation || currentAudio !== audio) return;
          stop();
          options.reportPlaybackError(error);
          onFinished();
        }

        try {
          const source = index === 0 ? alarmSoundUrl : vibrationSoundUrl;
          audio = options.createAudio?.(source) ?? new Audio(source);
          currentAudio = audio;
          audio.loop = false;
          audio.onended = () => {
            if (playbackGeneration !== generation || currentAudio !== audio || !audio) return;
            audio.onended = null;
            audio.onerror = null;
            currentAudio = null;
            if (timeout !== null) clearTimeout(timeout);
            timeout = null;
            playSegment(index + 1);
          };
          audio.onerror = () => fail(new Error("提醒短音效无法加载或播放"));
          // 两个素材都不到三秒；加载卡住时有界结束，避免占住后续反馈。
          timeout = setTimeout(() => fail(new Error("提醒短音效播放超时")), 5000);
          void audio.play().catch(fail);
        } catch (error) {
          fail(error);
        }
      }

      playSegment(0);
    }
  };
}
