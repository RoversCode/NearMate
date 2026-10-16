export type Live2dLipSyncSpeechState = {
  speechActive: boolean;
  speechSilenceFrameCount: number;
};

/**
 * 创建初始语音状态。
 *
 * Returns:
 *   尚未检测到语音的状态快照。
 */
export function createLive2dLipSyncSpeechState(): Live2dLipSyncSpeechState {
  return {
    speechActive: false,
    speechSilenceFrameCount: 0
  };
}

/**
 * 根据当前嘴型音量推进语音活跃状态。
 *
 * Args:
 *   state: 上一帧语音状态。
 *   audioMouthValue: 当前音频映射出的嘴型开合值。
 *   silenceFrameLimit: 允许保留语音态的连续静音帧数。
 *
 * Returns:
 *   下一帧状态和当前是否应按“说话中”处理。
 */
export function updateLive2dLipSyncSpeechState(
  state: Live2dLipSyncSpeechState,
  audioMouthValue: number,
  silenceFrameLimit: number
): {
  state: Live2dLipSyncSpeechState;
  speechActive: boolean;
} {
  if (audioMouthValue > 0) {
    return {
      state: {
        speechActive: true,
        speechSilenceFrameCount: 0
      },
      speechActive: true
    };
  }

  if (!state.speechActive) {
    return {
      state,
      speechActive: false
    };
  }

  const speechSilenceFrameCount = state.speechSilenceFrameCount + 1;
  if (speechSilenceFrameCount >= silenceFrameLimit) {
    return {
      state: {
        speechActive: false,
        speechSilenceFrameCount
      },
      speechActive: false
    };
  }

  return {
    state: {
      speechActive: true,
      speechSilenceFrameCount
    },
    speechActive: true
  };
}
