import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import { DeepSeekAvatarController } from "./deepSeekAvatarController";
import type { Live2dLipSyncModelBinding } from "./live2dLipSync";

const MODEL_PATH = "./live2d/resources/DeepSeekChibi/c_0120.model3.json";
const PARAMETER_IDS = ["ParamMouthOpenY", "ParamCheek16", "ParamCheek74", "ParamCheek76", "ParamMouthForm", "pointZ", "phone", "ji", "jingyu"];

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline; });
  return { promise, resolve, reject };
}

function createModelBinding(modelPath = MODEL_PATH) {
  const events = new EventEmitter();
  const motions = new EventEmitter();
  const values = new Float32Array(PARAMETER_IDS.length);
  const defaults = new Float32Array(PARAMETER_IDS.length);
  const motionManager = Object.assign(motions, {
    loadMotion: vi.fn(async (_group: string, _index: number): Promise<unknown> => ({})),
    stopAllMotions: vi.fn()
  });
  const model = {
    autoFocus: true,
    autoHitTest: true,
    destroyed: false,
    motion: vi.fn(async (_group: string, _index: number, _priority: number, _options: { loop: boolean }) => true),
    internalModel: Object.assign(events, {
      getIdSafe: (id: string) => id,
      coreModel: {
        getModel: () => ({ parameters: { values, defaultValues: defaults } }),
        saveParameters: vi.fn(),
        setParameterValueById: vi.fn((id: string, value: number, _weight: number) => {
          const index = PARAMETER_IDS.indexOf(id);
          if (index >= 0) values[index] = value;
        })
      },
      motionManager
    })
  };
  const binding = { model, modelPath, ticker: { add: vi.fn(), remove: vi.fn() } } satisfies Live2dLipSyncModelBinding;
  return { binding, model, events, motions, values, motionManager };
}

async function flushActions() {
  // 模拟资源加载与引擎启动经过的微任务，不让测试依赖真实动画时间。
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

describe("DeepSeekAvatarController", () => {
  it("只绑定实际提交的大肥鱼路径，不改动其他模型", () => {
    const controller = new DeepSeekAvatarController();
    const other = createModelBinding("./live2d/resources/Haru/Haru.model3.json");
    expect(controller.setModelBinding(other.binding)).toBe(false);
    controller.reactToTap();
    controller.playNextReaction();
    expect(other.motionManager.stopAllMotions).not.toHaveBeenCalled();
    expect(other.model.motion).not.toHaveBeenCalled();
    expect(other.model.autoFocus).toBe(true);
  });

  it("状态表情只在渲染前写入，不覆盖声音控制的嘴部开合", () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    expect(controller.setModelBinding(fixture.binding)).toBe(true);
    controller.setState("listening");
    fixture.values[0] = 0.72;
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[0]).toBeCloseTo(0.72);
    expect(fixture.values[1]).toBe(1);
    controller.setState("thinking");
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[1]).toBe(0);
    expect(fixture.values[2]).toBe(1);
    controller.setState("working");
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[2]).toBe(0);
    expect(fixture.values[5]).toBe(1);
    controller.setState("responding");
    fixture.values[0] = 0.41;
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[0]).toBeCloseTo(0.41);
    expect(fixture.values[3]).toBe(1);
    expect(fixture.values[4]).toBe(1);
  });

  it("轻点播放一次Hammer，完成后清除道具并恢复当前状态", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    controller.setModelBinding(fixture.binding);
    controller.setState("thinking");
    controller.reactToTap();
    await flushActions();
    expect(fixture.model.motion).toHaveBeenCalledWith("Hammer", 0, 3, { loop: false });
    fixture.values[6] = 1;
    fixture.motions.emit("motionFinish");
    await flushActions();
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[6]).toBe(0);
    expect(fixture.values[2]).toBe(1);
  });

  it("自拍先完整掏出手机，再开始Selfie；最后清掉手机状态", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    controller.setModelBinding(fixture.binding);
    controller.playNextReaction();
    await flushActions();
    controller.playNextReaction();
    await flushActions();
    expect(fixture.model.motion.mock.calls.map(call => call[0])).toEqual(["Hammer", "OpenCase"]);
    fixture.values[6] = 1;
    fixture.motions.emit("motionFinish");
    await flushActions();
    expect(fixture.model.motion.mock.lastCall?.[0]).toBe("Selfie");
    expect(fixture.values[6]).toBe(1);
    fixture.motions.emit("motionFinish");
    await flushActions();
    expect(fixture.values[6]).toBe(0);
  });

  it("依序提供全部七种动作，喷水带鲸鱼，番茄酱保留原曲线", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    controller.setModelBinding(fixture.binding);
    for (let index = 0; index < 7; index += 1) {
      controller.playNextReaction();
      await flushActions();
      if (index === 1) {
        fixture.motions.emit("motionFinish");
        await flushActions();
      }
      fixture.values[7] = 0.45;
      fixture.events.emit("beforeModelUpdate");
      expect(fixture.values[7]).toBeCloseTo(0.45);
      if (index === 5) expect(fixture.values[8]).toBe(1);
      fixture.motions.emit("motionFinish");
      await flushActions();
      expect(fixture.values[8]).toBe(0);
    }
    expect(fixture.model.motion.mock.calls.map(call => call[0])).toEqual([
      "Hammer", "OpenCase", "Selfie", "SelfieQuick", "Ketchup", "BubbleGum", "SprayWater", "OpenCase"
    ]);
    expect(fixture.model.motion.mock.calls.every(call => call[3].loop === false)).toBe(true);
  });

  it("说话时跳过吹泡泡，但允许不会遮挡嘴部的道具动作", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    controller.setModelBinding(fixture.binding);
    controller.setState("responding");
    for (let index = 0; index < 5; index += 1) {
      controller.playNextReaction();
      await flushActions();
    }
    expect(fixture.model.motion.mock.calls.map(call => call[0])).not.toContain("BubbleGum");
    expect(fixture.model.motion.mock.lastCall?.[0]).toBe("SprayWater");
    fixture.values[0] = 0.8;
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[0]).toBeCloseTo(0.8);
  });

  it("状态打断发生在动作资源返回前，迟到结果不能复活旧动作", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    const load = deferred<unknown>();
    fixture.motionManager.loadMotion.mockReturnValueOnce(load.promise);
    controller.setModelBinding(fixture.binding);
    controller.reactToTap();
    controller.setState("listening");
    load.resolve({});
    await flushActions();
    expect(fixture.model.motion).not.toHaveBeenCalled();
    fixture.events.emit("beforeModelUpdate");
    expect(fixture.values[1]).toBe(1);
  });

  it("连续点按在旧启动未返回时，只让最后一次动作接管", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    const start = deferred<boolean>();
    fixture.model.motion.mockReturnValueOnce(start.promise);
    controller.setModelBinding(fixture.binding);
    controller.reactToTap();
    await flushActions();
    controller.playNextReaction();
    controller.playNextReaction();
    await flushActions();
    expect(fixture.model.motion).toHaveBeenCalledTimes(1);
    start.resolve(true);
    await flushActions();
    expect(fixture.model.motion.mock.calls.map(call => call[0])).toEqual(["Hammer", "OpenCase"]);
    fixture.motions.emit("motionFinish");
    await flushActions();
    expect(fixture.model.motion.mock.lastCall?.[0]).toBe("Selfie");
  });

  it("取消恰好发生在掏手机完成后，不再继续自拍", async () => {
    const controller = new DeepSeekAvatarController();
    const fixture = createModelBinding();
    controller.setModelBinding(fixture.binding);
    controller.playNextReaction();
    await flushActions();
    controller.playNextReaction();
    await flushActions();
    fixture.motions.emit("motionFinish");
    controller.setState("responding");
    await flushActions();
    expect(fixture.model.motion.mock.calls.map(call => call[0])).not.toContain("Selfie");
  });

  it("切换模型会清理订阅，旧加载及完成事件不污染新模型", async () => {
    const controller = new DeepSeekAvatarController();
    const old = createModelBinding();
    const next = createModelBinding();
    const load = deferred<unknown>();
    old.motionManager.loadMotion.mockReturnValueOnce(load.promise);
    controller.setModelBinding(old.binding);
    controller.reactToTap();
    controller.setModelBinding(null);
    expect(old.events.listenerCount("beforeModelUpdate")).toBe(0);
    expect(old.motions.listenerCount("motionFinish")).toBe(0);
    expect(old.model.autoFocus).toBe(true);
    controller.setModelBinding(next.binding);
    controller.setState("thinking");
    old.model.destroyed = true;
    load.resolve({});
    old.motions.emit("motionFinish");
    await flushActions();
    expect(old.model.motion).not.toHaveBeenCalled();
    expect(next.model.motion).not.toHaveBeenCalled();
    next.events.emit("beforeModelUpdate");
    expect(next.values[2]).toBe(1);
  });

  it("加载失败或启动拒绝会报告一次并恢复待机，后续动作仍可用", async () => {
    const diagnostic = vi.fn();
    const controller = new DeepSeekAvatarController({ reportDiagnostic: diagnostic });
    const fixture = createModelBinding();
    controller.setModelBinding(fixture.binding);
    fixture.motionManager.loadMotion.mockRejectedValueOnce(new Error("missing"));
    controller.reactToTap();
    await flushActions();
    expect(diagnostic).toHaveBeenCalledTimes(1);
    fixture.model.motion.mockResolvedValueOnce(false);
    controller.reactToTap();
    await flushActions();
    expect(diagnostic).toHaveBeenCalledTimes(2);
    controller.reactToTap();
    await flushActions();
    expect(fixture.model.motion.mock.lastCall?.[0]).toBe("Hammer");
  });

  it("重复销毁安全，迟到失败不写已销毁模型或发出误导诊断", async () => {
    const diagnostic = vi.fn();
    const controller = new DeepSeekAvatarController({ reportDiagnostic: diagnostic });
    const fixture = createModelBinding();
    const load = deferred<unknown>();
    fixture.motionManager.loadMotion.mockReturnValueOnce(load.promise);
    controller.setModelBinding(fixture.binding);
    controller.reactToTap();
    controller.destroy();
    controller.destroy();
    fixture.model.destroyed = true;
    load.reject(new Error("late failure"));
    await flushActions();
    expect(diagnostic).not.toHaveBeenCalled();
    expect(fixture.model.motion).not.toHaveBeenCalled();
    expect(fixture.events.listenerCount("beforeModelUpdate")).toBe(0);
  });
});
