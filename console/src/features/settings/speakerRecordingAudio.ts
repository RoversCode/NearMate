const TARGET_SAMPLE_RATE = 16000;

/**
 * 把浏览器音频缓冲转换为 16kHz PCM16。
 *
 * 参数：
 *   channelData: 浏览器录音得到的单声道浮点采样。
 *   sourceSampleRate: 原始采样率。
 *
 * 返回：
 *   16kHz 小端序 PCM16 字节。
 */
export function float32ToPcm16(channelData: Float32Array, sourceSampleRate: number): ArrayBuffer {
  const resampled = resampleFloat32(channelData, sourceSampleRate, TARGET_SAMPLE_RATE);
  const pcm16 = new Int16Array(resampled.length);
  for (let index = 0; index < resampled.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, resampled[index]));
    pcm16[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm16.buffer;
}

function resampleFloat32(input: Float32Array, sourceSampleRate: number, targetSampleRate: number): Float32Array {
  if (sourceSampleRate === targetSampleRate) {
    return input;
  }

  const ratio = sourceSampleRate / targetSampleRate;
  const outputLength = Math.max(1, Math.floor(input.length / ratio));
  const output = new Float32Array(outputLength);
  for (let index = 0; index < outputLength; index += 1) {
    const sourceIndex = Math.min(input.length - 1, Math.floor(index * ratio));
    output[index] = input[sourceIndex];
  }
  return output;
}
