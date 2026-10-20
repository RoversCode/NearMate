import "pixi.js/unsafe-eval";
import { Application } from "pixi.js";
import { loadCubism5Live2dEngine } from "../src/renderer/src/lib/live2dRuntime";
import { loadLive2dStageModel } from "../src/renderer/src/components/live2dStageModelLoader";
import { syncLoadedAvatarStageModelLayout } from "../src/renderer/src/components/avatarStageLayout";
import { getAvatarPresentationProfile } from "../src/shared/avatarPresentationProfile";
import { DeepSeekAvatarController } from "../src/renderer/src/lib/deepSeekAvatarController";
import { Live2dLipSyncController } from "../src/renderer/src/lib/live2dLipSync";

const report = { suite: "integration", states: [], motions: [], diagnostics: [] };
const whalePath = "/live2d/resources/DeepSeekChibi/c_0120.model3.json";
const haruPath = "/live2d/resources/Haru/Haru.model3.json";
const reactionOrder = ["Hammer", "Selfie", "SelfieQuick", "Ketchup", "BubbleGum", "SprayWater", "OpenCase"];
let app;
let latestParameters = {};
let sampleFrame = null;
let latestMotionGroup;
let frameSequence = 0;

/** 条件不满足就终止回归；错误会随已完成的步骤一起写进报告。 */
function check(condition, message) {
  if (!condition) throw new Error(message);
}

/** 等到真实渲染条件满足；超时会报告正在等待的具体阶段。 */
async function until(condition, description, timeoutMs = 5000) {
  const deadline = performance.now() + timeoutMs;
  do {
    await new Promise(resolve => requestAnimationFrame(resolve));
    if (condition()) return;
  } while (performance.now() < deadline);
  throw new Error(`等待超时：${description}`);
}

/** 让模型实际绘制指定时间，不能用直接改动参数来冒充控制器播放。 */
async function renderFor(milliseconds) {
  const deadline = performance.now() + milliseconds;
  await until(() => performance.now() >= deadline, `渲染 ${milliseconds} 毫秒`, milliseconds + 3000);
}

/** 保存当前产品窗口尺寸的截图，供人工核对构图、状态和道具。 */
async function capture(name) {
  await fetch(`/capture/${name}?width=${app.screen.width}&height=${app.screen.height}`);
}

/** 读取最终 Core 输入，避免把更新结束后恢复的动作基线误当成可见状态。 */
function observeModel(model) {
  const core = model.internalModel.coreModel;
  const parameters = core.getModel().parameters;
  const originalUpdate = core.update.bind(core);
  core.update = () => {
    latestParameters = Object.fromEntries(parameters.ids.map((id, index) => [id, parameters.values[index]]));
    latestMotionGroup = model.internalModel.motionManager.state.currentGroup;
    frameSequence++;
    const result = originalUpdate();
    sampleFrame?.(latestParameters);
    return result;
  };
}

/** 记录完整绘制数据，用于判断表情确实改变了可见图层或顶点。 */
function drawableSnapshot(model) {
  const drawables = model.internalModel.coreModel.getModel().drawables;
  return [...drawables.opacities, ...drawables.vertexPositions.flatMap(values => [...values])];
}

/** 通过产品展示档案设置窗口、模型和交互边界，避免测试自建另一套布局。 */
function applyProductLayout(model, modelId) {
  const profile = getAvatarPresentationProfile(modelId);
  app.renderer.resize(profile.windowWidth, profile.windowHeight);
  model.anchor.set(0.5, 1);
  let bounds;
  const layout = syncLoadedAvatarStageModelLayout({
    app, mode: "pet", model, presentationProfile: profile,
    onPetBoundsChange: value => { bounds = value; }
  });
  check(bounds && bounds.width > 0 && bounds.height > 0, `${modelId} 没有有效交互区域`);
  check(bounds.x >= 0 && bounds.y >= 0
    && bounds.x + bounds.width <= app.screen.width
    && bounds.y + bounds.height <= app.screen.height, `${modelId} 交互区域超出窗口`);
  return { modelId, window: { width: app.screen.width, height: app.screen.height }, bounds, layout };
}

/** 准备真正经过 Web Audio 的输入；未指定 WAV 时明确使用合成测试信号。 */
async function prepareAudio(lipSync) {
  const config = await (await fetch("/verification.json")).json();
  const context = new AudioContext();
  const destination = context.createMediaStreamDestination();
  let buffer;
  if (config.speech) {
    buffer = await context.decodeAudioData(await (await fetch("/speech.wav")).arrayBuffer());
  } else {
    buffer = context.createBuffer(1, context.sampleRate * 2, context.sampleRate);
    const values = buffer.getChannelData(0);
    for (let index = 0; index < values.length; index++) {
      const time = index / context.sampleRate;
      const envelope = Math.sin(time * Math.PI * 4) ** 2;
      values[index] = 0.2 * envelope * Math.sin(time * Math.PI * 440);
    }
  }
  const element = document.createElement("audio");
  element.srcObject = destination.stream;
  lipSync.setAudioElement(element);
  await context.resume();
  return {
    context, destination, buffer,
    kind: config.speech ? "provided-wav" : "synthetic-amplitude-signal",
    start() {
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(destination);
      source.start();
      return source;
    }
  };
}

/**
 * 运行产品大肥鱼控制器、口型控制器和展示档案的真实集成回归。
 *
 * Args:
 *   无；从正式 public 资源加载模型，通过 --speech 可选择本地 WAV。
 *
 * Returns:
 *   无；截图和结构化报告由 Electron 启动器保存。
 */
async function runIntegration() {
  console.assert = (condition, ...details) => check(condition, `Cubism assert: ${details.join(" ")}`);
  const engine = await loadCubism5Live2dEngine();
  report.engine = engine.VERSION;
  app = new Application();
  await app.init({ width: 480, height: 480, preference: "webgl", backgroundAlpha: 0, antialias: true });
  document.body.appendChild(app.canvas);
  const model = await loadLive2dStageModel({ app, modelPath: whalePath });
  app.stage.addChild(model);
  report.whaleLayout = applyProductLayout(model, "DeepSeekChibi");
  observeModel(model);
  const controller = new DeepSeekAvatarController({
    reportDiagnostic: (level, message) => report.diagnostics.push({ level, message })
  });
  const binding = { model, ticker: app.ticker, modelPath: whalePath };
  check(controller.setModelBinding(binding), "正式大肥鱼路径未能绑定控制器");
  const lipSync = new Live2dLipSyncController({
    reportDiagnostic: (level, message) => {
      // 口型模块的绑定和采样记录也使用 warn 等级；只有失败诊断表示回归失败。
      if (/failed|unsupported|missing/.test(message)) report.diagnostics.push({ level, message });
    }
  });
  lipSync.setModelBinding(binding);
  const audio = await prepareAudio(lipSync);
  controller.setState("available");
  await renderFor(250);
  await capture("whale-idle");
  const neutralParameters = { ...latestParameters };
  const expectedStates = {
    listening: { ParamCheek16: 1 }, thinking: { ParamCheek74: 1 },
    responding: { ParamCheek76: 1, ParamMouthForm: 1 }, working: { pointZ: 1 }
  };
  for (const [state, parameters] of Object.entries(expectedStates)) {
    // 静止 ticker 后比较同一默认姿态，排除 Idle 呼吸、物理抖动造成的顶点差异。
    app.ticker.stop();
    controller.setState("available");
    model.internalModel.emit("beforeModelUpdate");
    model.internalModel.coreModel.update();
    const neutral = drawableSnapshot(model);
    controller.setState(state);
    model.internalModel.emit("beforeModelUpdate");
    model.internalModel.coreModel.update();
    const stateDrawable = drawableSnapshot(model);
    const staticChanged = stateDrawable.reduce((count, value, index) =>
      count + (Math.abs(value - neutral[index]) > 0.001 ? 1 : 0), 0);
    check(staticChanged > 0, `${state} 未独立改变任何可见图层或顶点`);
    app.ticker.start();
    await renderFor(250);
    for (const [id, value] of Object.entries(parameters)) {
      check(Math.abs(latestParameters[id] - value) < 0.001, `${state} 的 ${id} 未实际生效`);
    }
    const changedStateParameters = Object.keys(parameters)
      .filter(id => Math.abs(latestParameters[id] - neutralParameters[id]) > 0.001);
    await capture(`state-${state}`);
    report.states.push({ state, parameters,
      idleParameters: Object.fromEntries(Object.keys(parameters).map(id => [id, neutralParameters[id]])),
      changedStateParameters, staticChangedDrawableValues: staticChanged });
    check(changedStateParameters.length > 0, `${state} 的指定参数全部等于待机值，未产生独立状态变化`);
  }
  controller.setState("available");
  const manifest = await (await fetch(whalePath)).json();
  const motions = {};
  for (const [group, entries] of Object.entries(manifest.FileReferences.Motions)) {
    motions[group] = await (await fetch(new URL(entries[0].File, location.origin + whalePath))).json();
  }
  const manager = model.internalModel.motionManager;
  const starts = [];
  manager.on("motionStart", group => starts.push({ group, time: performance.now() }));
  const allParameters = model.internalModel.coreModel.getModel().parameters;
  const defaults = Object.fromEntries(allParameters.ids.map((id, index) => [id, allParameters.defaultValues[index]]));
  const idleIds = new Set(motions.Idle.Curves.map(curve => curve.Id));
  const propIds = [...new Set(Object.values(motions).flatMap(motion => motion.Curves.map(curve => curve.Id)))]
    .filter(id => !idleIds.has(id) && (!id.startsWith("Param") || ["ParamCheek20", "ParamCheek27", "ParamCheek75", "Param70", "Param71", "Param72"].includes(id)));
  for (const group of reactionOrder) {
    console.log(`LIVE2D_PROGRESS 验证动作 ${group}`);
    const firstStart = starts.length;
    const curveIds = motions[group].Curves.map(curve => curve.Id);
    const ranges = Object.fromEntries(curveIds.map(id => [id, { min: Infinity, max: -Infinity }]));
    const mouth = [];
    sampleFrame = parameters => {
      if (manager.state.currentGroup !== group) return;
      mouth.push(parameters.ParamMouthOpenY);
      for (const [id, range] of Object.entries(ranges)) {
        range.min = Math.min(range.min, parameters[id]);
        range.max = Math.max(range.max, parameters[id]);
      }
    };
    const source = group === "Hammer" ? audio.start() : null;
    controller.playNextReaction();
    await until(() => starts.slice(firstStart).some(start => start.group === group), `${group} 启动`);
    const groupStart = starts.slice(firstStart).find(start => start.group === group).time;
    await until(() => performance.now() - groupStart > motions[group].Meta.Duration * 400, `${group} 可见动作帧`);
    await capture(`motion-${group.toLowerCase()}`);
    await until(() => manager.state.currentGroup === "Idle", `${group} 自然回到 Idle`, 9000);
    await renderFor(150);
    const changed = Object.entries(ranges).filter(([, range]) => range.max - range.min > 0.01).map(([id]) => id);
    check(changed.length > 0, `${group} 没有实际改变动作曲线参数`);
    const leftovers = propIds.filter(id => Math.abs(latestParameters[id] - defaults[id]) > 0.001);
    check(leftovers.length === 0, `${group} 待机后道具未复位：${leftovers.join(", ")}`);
    const steps = starts.slice(firstStart).map(start => start.group);
    if (group === "Selfie") {
      check(steps.indexOf("OpenCase") >= 0 && steps.indexOf("OpenCase") < steps.indexOf("Selfie"), "自拍未先完整打开手机");
    }
    if (source) {
      source.stop();
      sampleFrame = parameters => mouth.push(parameters.ParamMouthOpenY);
      await renderFor(700);
      check(Math.max(...mouth) > 0.1, "实际音频未能在动作期间打开嘴部");
      check(latestParameters.ParamMouthOpenY < 0.1, "音频停止后嘴部没有闭合");
      report.audio = { kind: audio.kind, duration: audio.buffer.duration,
        maxMouth: Math.max(...mouth), stoppedMouth: latestParameters.ParamMouthOpenY, parallelAction: group };
    }
    sampleFrame = null;
    report.motions.push({ group, steps, changedCurveParameters: changed, propsReset: true });
  }
  report.stateCancellation = [];
  for (let attempt = 0; attempt < 8; attempt++) {
    controller.setState("available");
    controller.reactToTap();
    await until(() => manager.state.currentGroup === "Hammer", "取消测试动作启动");
    if (attempt % 2) await renderFor(250);
    const sequenceBeforeCancellation = frameSequence;
    controller.setState("listening");
    await until(() => manager.state.currentGroup === "Idle" && latestParameters.ParamCheek16 === 1, "状态变化取消动作");
    const earlyLeftovers = propIds.filter(id => Math.abs(latestParameters[id] - defaults[id]) > 0.001);
    // 动作 Promise 可在两帧之间更新 manager；必须等同一帧的 Core 数据，不能混用新状态和旧参数。
    const firstObservation = {
      managerGroup: manager.state.currentGroup, sampledGroup: latestMotionGroup,
      frameSequence, sequenceBeforeCancellation,
      leftovers: Object.fromEntries(earlyLeftovers.map(id => [id, { actual: latestParameters[id], expected: defaults[id] }]))
    };
    await until(() => frameSequence > sequenceBeforeCancellation
      && latestMotionGroup === "Idle" && latestParameters.ParamCheek16 === 1, "取消后真实 Idle 帧");
    const cancelledLeftovers = propIds.filter(id => Math.abs(latestParameters[id] - defaults[id]) > 0.001);
    check(cancelledLeftovers.length === 0, `第 ${attempt + 1} 次取消后道具未复位：${cancelledLeftovers.map(id => `${id}=${latestParameters[id]},默认=${defaults[id]}`).join("; ")}`);
    report.stateCancellation.push({ attempt, propsReset: true, firstObservation });
  }
  await capture("cancelled-listening");

  // 模型切换先解绑；保存旧引用检查控制器不再写入旧模型，也不向 Haru 注入专属参数。
  const haru = await loadLive2dStageModel({ app, modelPath: haruPath });
  controller.setModelBinding(null);
  lipSync.setModelBinding(null);
  check(!controller.setModelBinding({ model: haru, ticker: app.ticker, modelPath: haruPath }), "Haru 被误识别为大肥鱼");
  const haruCore = haru.internalModel.coreModel;
  const beforeHaru = new Float32Array(haruCore.getModel().parameters.values);
  controller.setState("thinking");
  controller.playNextReaction();
  check(beforeHaru.every((value, index) => value === haruCore.getModel().parameters.values[index]), "解绑后仍修改了 Haru 参数");
  check(model.internalModel.listenerCount("beforeModelUpdate") === 0, "旧模型仍保留控制器事件订阅");
  app.stage.removeChild(model);
  model.destroy();
  app.stage.addChild(haru);
  report.haruLayout = applyProductLayout(haru, "Haru");
  observeModel(haru);
  await renderFor(300);
  await capture("haru-after-switch");
  report.switchToHaru = true;
  controller.destroy();
  lipSync.destroy();
  audio.destination.stream.getTracks().forEach(track => track.stop());
  await audio.context.close();
  app.stage.removeChild(haru);
  haru.destroy();
  app.destroy(true, true);
  check(report.diagnostics.length === 0, "控制器产生了警告诊断");
  console.log("LIVE2D_LIFECYCLE_REPORT " + JSON.stringify(report));
}

runIntegration().catch(error => {
  console.log("LIVE2D_LIFECYCLE_REPORT " + JSON.stringify({ ...report, failure: error.stack ?? String(error) }));
});
