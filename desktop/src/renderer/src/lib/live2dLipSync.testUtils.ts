import { vi } from "vitest";

import type {
  Live2dLipSyncModel,
  Live2dLipSyncTicker
} from "./live2dLipSync";

export function createTicker(): Live2dLipSyncTicker & { tick(): void } {
  const callbacks = new Set<() => void>();

  return {
    add(callback) {
      callbacks.add(callback);
    },
    remove(callback) {
      callbacks.delete(callback);
    },
    tick() {
      for (const callback of callbacks) {
        callback();
      }
    }
  };
}

export function createModel(lipSyncIds: string[] = ["ParamMouthOpenY"]) {
  const addParameterValueById = vi.fn();
  const parameterValues = new Map<string, number>();
  const setParameterValueById = vi.fn((id: string, value: number) => {
    parameterValues.set(id, value);
  });
  const getParameterValueById = vi.fn((id: string) => parameterValues.get(id) ?? 0);
  const getIdSafe = vi.fn((id: string) => `safe:${id}`);
  const listeners = new Map<string, Set<() => void>>();
  const on = vi.fn((eventName: string, listener: () => void) => {
    const eventListeners = listeners.get(eventName) ?? new Set<() => void>();
    eventListeners.add(listener);
    listeners.set(eventName, eventListeners);
  });
  const off = vi.fn((eventName: string, listener: () => void) => {
    listeners.get(eventName)?.delete(listener);
  });
  const model: Live2dLipSyncModel = {
    internalModel: {
      coreModel: {
        addParameterValueById,
        getParameterValueById,
        setParameterValueById
      },
      getIdSafe,
      motionManager: {
        lipSyncIds
      },
      off,
      on,
      options: {
        lipSyncGain: 1.5,
        lipSyncWeight: 0.4
      }
    }
  };

  return {
    addParameterValueById,
    emit(eventName: string) {
      for (const listener of listeners.get(eventName) ?? []) {
        listener();
      }
    },
    getParameterValueById,
    getIdSafe,
    off,
    on,
    model,
    parameterValues,
    setParameterValueById
  };
}

export function createLegacyModel(lipSyncIds: string[] = ["PARAM_MOUTH_OPEN_Y"]) {
  const addToParamFloat = vi.fn();
  const parameterValues = new Map<number, number>();
  const setParamFloat = vi.fn((index: number, value: number) => {
    parameterValues.set(index, value);
  });
  const getParamFloat = vi.fn((index: number) => parameterValues.get(index) ?? 0);
  const getParamIndex = vi.fn((id: string) => id.length);
  const model: Live2dLipSyncModel = {
    internalModel: {
      coreModel: {
        addToParamFloat,
        getParamFloat,
        getParamIndex,
        setParamFloat
      },
      motionManager: {
        lipSyncIds
      },
      options: {
        lipSyncGain: 1.5,
        lipSyncWeight: 0.4
      }
    }
  };

  return {
    addToParamFloat,
    getParamFloat,
    getParamIndex,
    model,
    setParamFloat
  };
}

export function createModelWithOptions(options: Record<string, unknown>) {
  const fixture = createModel(["ParamMouthOpenY"]);
  if (fixture.model.internalModel) {
    fixture.model.internalModel.options = options;
  }

  return fixture;
}

export function createAudioContextFixture(
  samples: number[],
  options: {
    mediaStreamSource?: boolean;
    state?: AudioContextState;
  } = {}
) {
  const sourceConnect = vi.fn();
  const sourceDisconnect = vi.fn();
  const analyserConnect = vi.fn();
  const analyserDisconnect = vi.fn();
  const close = vi.fn(async () => undefined);
  const resume = vi.fn(async () => undefined);
  const analyser = {
    fftSize: samples.length,
    minDecibels: -90,
    maxDecibels: -10,
    smoothingTimeConstant: 0.85,
    connect: analyserConnect,
    disconnect: analyserDisconnect,
    getFloatTimeDomainData(buffer: Float32Array) {
      samples.forEach((sample, index) => {
        buffer[index] = sample;
      });
    }
  };
  const streamSourceConnect = vi.fn();
  const streamSourceDisconnect = vi.fn();
  const audioContext = {
    close,
    createAnalyser: vi.fn(() => analyser),
    createMediaElementSource: vi.fn(() => ({
      connect: sourceConnect,
      disconnect: sourceDisconnect
    })),
    createMediaStreamSource: options.mediaStreamSource
      ? vi.fn(() => ({
          connect: streamSourceConnect,
          disconnect: streamSourceDisconnect
        }))
      : undefined,
    destination: {},
    resume,
    state: options.state ?? "running"
  };

  return {
    analyserConnect,
    analyserDisconnect,
    audioContext,
    close,
    resume,
    sourceConnect,
    sourceDisconnect,
    streamSourceConnect,
    streamSourceDisconnect
  };
}
