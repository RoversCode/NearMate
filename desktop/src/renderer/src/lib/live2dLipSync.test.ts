// @vitest-environment jsdom

import { describe, expect, it, vi } from "vitest";

import {
  Live2dLipSyncController,
  type Live2dLipSyncModel
} from "./live2dLipSync";
import {
  createAudioContextFixture,
  createModel,
  createModelWithOptions,
  createTicker
} from "./live2dLipSync.testUtils";

describe("Live2dLipSyncController", () => {
  it("LiveKit audio 元素带 MediaStream 时优先从 srcObject 创建音频分析源", () => {
    const {
      analyserConnect,
      audioContext,
      sourceConnect,
      streamSourceConnect
    } = createAudioContextFixture([0.03, -0.03, 0.03, -0.03], {
      mediaStreamSource: true
    });
    const audioElement = document.createElement("audio");
    const mediaStream = {
      getAudioTracks: () => [{ kind: "audio" }]
    } as unknown as MediaStream;
    Object.defineProperty(audioElement, "srcObject", {
      configurable: true,
      value: mediaStream
    });
    const reportDiagnostic = vi.fn();
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4,
      reportDiagnostic
    });

    controller.setAudioElement(audioElement);

    expect(audioContext.createMediaStreamSource).toHaveBeenCalledWith(mediaStream);
    expect(audioContext.createMediaElementSource).not.toHaveBeenCalled();
    expect(streamSourceConnect).toHaveBeenCalledWith(audioContext.createAnalyser.mock.results[0]?.value);
    expect(sourceConnect).not.toHaveBeenCalled();
    expect(analyserConnect).not.toHaveBeenCalled();
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.live2d_lip_sync_audio_bound",
      expect.objectContaining({
        audioSourceKind: "media_stream",
        mediaStreamTrackCount: 1
      })
    );
  });

  it("优先挂到 Live2D 模型更新前事件，避免在 Pixi ticker 中提前污染参数基准", () => {
    const { audioContext } = createAudioContextFixture([0.5, -0.5, 0.5, -0.5]);
    const ticker = {
      add: vi.fn(),
      remove: vi.fn()
    };
    const { emit, model, on, setParameterValueById } = createModel(["ParamMouthOpenY"]);
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker
    });
    emit("beforeModelUpdate");

    expect(on).toHaveBeenCalledWith("beforeModelUpdate", expect.any(Function));
    expect(ticker.add).not.toHaveBeenCalled();
    expect(setParameterValueById).toHaveBeenCalledWith(
      "safe:ParamMouthOpenY",
      1,
      1
    );
  });

  it("音频元素和模型都绑定后，随 ticker 写入嘴型参数", () => {
    const { audioContext } = createAudioContextFixture([0.5, -0.5, 0.5, -0.5]);
    const ticker = createTicker();
    const { emit, model, setParameterValueById } = createModel(["ParamMouthOpenY"]);
    const audioElement = document.createElement("audio");
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(audioElement);
    controller.setModelBinding({
      model,
      ticker
    });
    emit("beforeModelUpdate");

    expect(setParameterValueById).toHaveBeenCalledWith(
      "safe:ParamMouthOpenY",
      1,
      1
    );
  });

  it("解绑音频或模型时停止 ticker 更新并断开 Web Audio 节点", () => {
    const {
      audioContext,
      close,
      sourceDisconnect,
      analyserDisconnect
    } = createAudioContextFixture([0.5, -0.5, 0.5, -0.5]);
    const ticker = createTicker();
    const { emit, model, setParameterValueById } = createModel(["ParamMouthOpenY"]);
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker
    });
    setParameterValueById.mockClear();
    controller.setAudioElement(null);
    emit("beforeModelUpdate");
    controller.destroy();

    expect(setParameterValueById.mock.calls.every(([, value]) => value === 0)).toBe(true);
    expect(sourceDisconnect).toHaveBeenCalledOnce();
    expect(analyserDisconnect).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it("嘴型写入异常时不会把模型更新事件继续向外抛出", () => {
    const { audioContext } = createAudioContextFixture([0.5, -0.5, 0.5, -0.5]);
    const { emit, model, off, setParameterValueById } = createModel(["ParamMouthOpenY"]);
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    setParameterValueById.mockImplementation(() => {
      throw new Error("parameter write failed");
    });
    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });

    expect(() => {
      emit("beforeModelUpdate");
    }).not.toThrow();
    expect(off).toHaveBeenCalledWith("beforeModelUpdate", expect.any(Function));
  });

  it("控制器复用模型 lipSyncGain 并报告前几帧嘴型诊断", () => {
    const { audioContext } = createAudioContextFixture([0.01, -0.01, 0.01, -0.01]);
    const { emit, model, setParameterValueById } = createModelWithOptions({
      lipSyncGain: 1.5,
      lipSyncWeight: 0.4
    });
    const reportDiagnostic = vi.fn();
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4,
      reportDiagnostic
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });
    emit("beforeModelUpdate");

    expect(setParameterValueById).toHaveBeenCalledWith(
      "safe:ParamMouthOpenY",
      expect.closeTo(Math.pow(0.9, 1.15), 3),
      1
    );
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.live2d_lip_sync_frame",
      expect.objectContaining({
        level: 0.6,
        mouthValue: expect.closeTo(Math.pow(0.9, 1.15), 3),
        parameterValueAfter: expect.closeTo(Math.pow(0.9, 1.15), 3),
        parameterValueBefore: 0,
        rms: expect.closeTo(0.01, 4)
      })
    );
  });

  it("静音时保留模型当前嘴部基准，避免把原表情张嘴硬拉成闭嘴", () => {
    const { audioContext } = createAudioContextFixture([0, 0, 0, 0]);
    const {
      emit,
      model,
      parameterValues,
      setParameterValueById
    } = createModelWithOptions({
      lipSyncGain: 1.5,
      lipSyncWeight: 0.4
    });
    parameterValues.set("safe:ParamMouthOpenY", 0.8);
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });
    emit("beforeModelUpdate");

    expect(setParameterValueById).toHaveBeenLastCalledWith(
      "safe:ParamMouthOpenY",
      0.8,
      1
    );
  });

  it("连续帧嘴型会平滑跟随音量变化，避免从闭嘴直接跳到最大张嘴", () => {
    const samples = [0, 0, 0, 0];
    const { audioContext } = createAudioContextFixture(samples);
    const {
      emit,
      model,
      setParameterValueById
    } = createModelWithOptions({
      lipSyncGain: 1.5,
      lipSyncWeight: 0.4
    });
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });
    emit("beforeModelUpdate");
    samples.splice(0, samples.length, 0.5, -0.5, 0.5, -0.5);
    emit("beforeModelUpdate");

    const lastCall = setParameterValueById.mock.calls.at(-1);
    expect(lastCall?.[0]).toBe("safe:ParamMouthOpenY");
    expect(lastCall?.[1]).toBeGreaterThan(0);
    expect(lastCall?.[1]).toBeLessThan(1);
  });

  it("说话过程中由音频主导嘴型，低音量帧不会被表情张嘴基准托住", () => {
    const samples = [0.5, -0.5, 0.5, -0.5];
    const { audioContext } = createAudioContextFixture(samples);
    const {
      emit,
      model,
      parameterValues,
      setParameterValueById
    } = createModelWithOptions({
      lipSyncGain: 1.5,
      lipSyncWeight: 0.4
    });
    parameterValues.set("safe:ParamMouthOpenY", 0.8);
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });
    emit("beforeModelUpdate");
    samples.splice(0, samples.length, 0, 0, 0, 0);
    parameterValues.set("safe:ParamMouthOpenY", 0.8);
    emit("beforeModelUpdate");

    const lastCall = setParameterValueById.mock.calls.at(-1);
    expect(lastCall?.[0]).toBe("safe:ParamMouthOpenY");
    expect(lastCall?.[1]).toBeLessThan(0.8);
  });

  it("启动期静音诊断不会耗尽后续有声音频帧诊断额度", () => {
    const samples = [0, 0, 0, 0];
    const { audioContext } = createAudioContextFixture(samples);
    const { emit, model } = createModelWithOptions({
      lipSyncGain: 1.5,
      lipSyncWeight: 0.4
    });
    const reportDiagnostic = vi.fn();
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4,
      reportDiagnostic
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });
    for (let frame = 0; frame < 5; frame += 1) {
      emit("beforeModelUpdate");
    }
    samples.splice(0, samples.length, 0.01, -0.01, 0.01, -0.01);
    emit("beforeModelUpdate");

    const voicedFrameDiagnostic = reportDiagnostic.mock.calls.find(([, message, details]) => {
      return (
        message === "avatar.live2d_lip_sync_frame" &&
        typeof details === "object" &&
        details !== null &&
        typeof Reflect.get(details, "rms") === "number" &&
        Reflect.get(details, "rms") > 0
      );
    });

    expect(voicedFrameDiagnostic?.[2]).toEqual(
      expect.objectContaining({
        mouthValue: expect.any(Number),
        rms: expect.closeTo(0.01, 4)
      })
    );
    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.live2d_lip_sync_frame",
      expect.objectContaining({
        rms: expect.closeTo(0.01, 4)
      })
    );
  });

  it("有音量但模型不支持写入时报告明确诊断并解绑更新事件", () => {
    const { audioContext } = createAudioContextFixture([0.01, -0.01, 0.01, -0.01]);
    const reportDiagnostic = vi.fn();
    const listeners = new Map<string, Set<() => void>>();
    const on = vi.fn((eventName: string, listener: () => void) => {
      listeners.set(eventName, new Set([listener]));
    });
    const off = vi.fn();
    const model: Live2dLipSyncModel = {
      internalModel: {
        coreModel: {},
        motionManager: {
          lipSyncIds: ["ParamMouthOpenY"]
        },
        off,
        on
      }
    };
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4,
      reportDiagnostic
    });

    controller.setAudioElement(document.createElement("audio"));
    controller.setModelBinding({
      model,
      ticker: createTicker()
    });
    for (const listener of listeners.get("beforeModelUpdate") ?? []) {
      listener();
    }

    expect(reportDiagnostic).toHaveBeenCalledWith(
      "warn",
      "avatar.live2d_lip_sync_model_unsupported",
      expect.objectContaining({
        reason: "missing_mouth_parameter_writer",
        writerKind: "none"
      })
    );
    expect(off).toHaveBeenCalledWith("beforeModelUpdate", expect.any(Function));
  });

  it("绑定播放元素时会恢复 suspended AudioContext，避免 analyser 一直读到静音", () => {
    const { audioContext, resume } = createAudioContextFixture(
      [0.03, -0.03, 0.03, -0.03],
      {
        state: "suspended"
      }
    );
    const controller = new Live2dLipSyncController({
      createAudioContext: () => audioContext,
      fftSize: 4
    });

    controller.setAudioElement(document.createElement("audio"));

    expect(resume).toHaveBeenCalledOnce();
  });
});
