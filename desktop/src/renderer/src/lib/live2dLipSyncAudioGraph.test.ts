import { describe, expect, it, vi } from "vitest";
import {
  createLive2dLipSyncAudioGraph,
  disconnectLive2dLipSyncAudioGraph
} from "./live2dLipSyncAudioGraph";
import type { Live2dLipSyncAudioContext } from "./live2dLipSyncTypes";

function createAudioContextStub(trackCount: number) {
  const analyser = {
    fftSize: 0,
    minDecibels: 0,
    maxDecibels: 0,
    smoothingTimeConstant: 0,
    connect: vi.fn(),
    disconnect: vi.fn(),
    getFloatTimeDomainData: vi.fn()
  };
  const mediaStreamSource = {
    connect: vi.fn(),
    disconnect: vi.fn()
  };
  const mediaElementSource = {
    connect: vi.fn(),
    disconnect: vi.fn()
  };
  const mediaStream = {
    getAudioTracks: vi.fn(() => Array.from({ length: trackCount }, () => ({})))
  };
  const element = {
    srcObject: mediaStream
  } as unknown as HTMLMediaElement;
  const audioContext: Live2dLipSyncAudioContext = {
    destination: {},
    state: "running",
    close: vi.fn(async () => undefined),
    createAnalyser: vi.fn(() => analyser),
    createMediaElementSource: vi.fn(() => mediaElementSource),
    createMediaStreamSource: vi.fn(() => mediaStreamSource)
  };

  return {
    analyser,
    audioContext,
    element,
    mediaElementSource,
    mediaStreamSource
  };
}

describe("live2dLipSyncAudioGraph", () => {
  it("LiveKit audio 元素带音轨时优先绑定 media stream source，并且不把 analyser 接回 destination", () => {
    const { analyser, audioContext, element, mediaStreamSource } = createAudioContextStub(1);

    const graph = createLive2dLipSyncAudioGraph({
      audioContext,
      element,
      fftSize: 512
    });

    expect(graph.sourceKind).toBe("media_stream");
    expect(graph.mediaStreamTrackCount).toBe(1);
    expect(graph.buffer).toBeInstanceOf(Float32Array);
    expect(graph.buffer).toHaveLength(512);
    expect(analyser.fftSize).toBe(512);
    expect(mediaStreamSource.connect).toHaveBeenCalledWith(analyser);
    expect(analyser.connect).not.toHaveBeenCalled();
  });

  it("没有 audio tracks 时回退到 media element source，并断开图时保持幂等", () => {
    const { analyser, audioContext, element, mediaElementSource } = createAudioContextStub(0);

    const graph = createLive2dLipSyncAudioGraph({
      audioContext,
      element,
      fftSize: 256
    });
    disconnectLive2dLipSyncAudioGraph(graph, () => undefined);

    expect(graph.sourceKind).toBe("media_element");
    expect(graph.mediaStreamTrackCount).toBe(0);
    expect(mediaElementSource.connect).toHaveBeenCalledWith(analyser);
    expect(analyser.connect).toHaveBeenCalledWith(audioContext.destination);
    expect(mediaElementSource.disconnect).toHaveBeenCalledOnce();
    expect(analyser.disconnect).toHaveBeenCalledOnce();
    expect(audioContext.close).toHaveBeenCalledOnce();
  });
});
