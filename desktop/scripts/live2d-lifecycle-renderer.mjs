import "pixi.js/unsafe-eval";
import { Application } from "pixi.js";
import { loadCubism5Live2dEngine } from "../src/renderer/src/lib/live2dRuntime";
import { loadLive2dStageModel } from "../src/renderer/src/components/live2dStageModelLoader";

const reportPrefix = "LIVE2D_LIFECYCLE_REPORT ";
const samples = [];

/**
 * 校验真实模型状态，失败时保留具体原因，避免浏览器 console.assert 只报警而测试仍通过。
 *
 * Args:
 *   condition: 必须成立的条件。
 *   message: 不满足时的说明。
 *
 * Returns:
 *   无；失败时抛出错误。
 */
function check(condition, message) {
  if (!condition) throw new Error(message);
}

/**
 * 等待下一次真实渲染帧，保证截图和模型销毁发生在绘制之后。
 *
 * Args:
 *   无。
 *
 * Returns:
 *   下一帧开始时完成的 Promise。
 */
function nextFrame() {
  return new Promise(resolve => requestAnimationFrame(resolve));
}

/**
 * 经产品加载入口反复加载模型、更新嘴部、绘制，再验证 Model 与 Moc 的释放次序和引用计数。
 *
 * Args:
 *   无；模型 URL 由隔离测试服务提供。
 *
 * Returns:
 *   无；结果交给 Electron 父进程保存。
 */
async function verifyLifecycle() {
  console.assert = (condition, ...details) => check(condition, `Cubism assert: ${details.join(" ")}`);
  const engine = await loadCubism5Live2dEngine();
  const app = new Application();
  await app.init({ width: 720, height: 720, preference: "webgl", backgroundAlpha: 0, antialias: true });
  document.body.appendChild(app.canvas);
  const modelUrls = await (await fetch("/models.json")).json();
  for (let cycle = 0; cycle < 5; cycle++) {
    for (let index = 0; index < modelUrls.length; index++) {
      const model = await loadLive2dStageModel({ app, modelPath: modelUrls[index] });
      const internal = model.internalModel;
      const core = internal.coreModel;
      const moc = internal.__moc;
      check(moc?._modelCount === 1, "创建模型后 Moc 引用数必须为 1");
      const releases = [];
      const originalDeleteModel = moc.deleteModel.bind(moc);
      const originalReleaseMoc = moc.release.bind(moc);
      const originalReleaseCore = core.release.bind(core);
      core.release = () => {
        releases.push("core.release");
        return originalReleaseCore();
      };
      moc.deleteModel = target => {
        check(target === core, "释放的模型必须与本次创建的模型相同");
        releases.push("moc.deleteModel");
        originalDeleteModel(target);
        check(moc._modelCount === 0, "deleteModel 必须同步递减 Moc 引用数");
      };
      moc.release = () => {
        check(moc._modelCount === 0, "释放 Moc 前仍存在存活模型");
        releases.push("moc.release");
        return originalReleaseMoc();
      };
      model.anchor.set(0.5);
      model.scale.set(Math.min(680 / model.width, 680 / model.height));
      model.position.set(360, 360);
      app.stage.addChild(model);
      let mouthFrames = 0;
      let mouthAtCoreUpdate = 0;
      const originalUpdateCore = core.update.bind(core);
      core.update = () => {
        mouthAtCoreUpdate = core.getParameterValueById(internal.getIdSafe("ParamMouthOpenY"));
        return originalUpdateCore();
      };
      // 写入时机与产品口型控制器一致，确认升级没有移动或丢失该事件。
      internal.on("beforeModelUpdate", () => {
        core.setParameterValueById(internal.getIdSafe("ParamMouthOpenY"), 0.7, 1);
        mouthFrames++;
      });
      for (let frame = 0; frame < 8; frame++) await nextFrame();
      check(mouthFrames > 0, "模型更新必须触发 beforeModelUpdate");
      // 引擎更新完顶点会恢复动作参数，因此在 Core 消费参数的时刻取样。
      check(mouthAtCoreUpdate > 0.5, "嘴部参数未能送进 Core 更新");
      check(model.textures.length > 0, "模型贴图未加载");
      if (cycle === 0) await fetch(`/capture/${index}`);
      const sample = {
        cycle, model: modelUrls[index], textures: model.textures.length,
        drawables: core.getDrawableCount(), mouthFrames, mouthAtCoreUpdate,
        bounds: { width: model.width, height: model.height }
      };
      app.stage.removeChild(model);
      model.destroy();
      check(releases.join(",") === "moc.deleteModel,core.release,moc.release", `释放顺序异常：${releases}`);
      check(moc._moc === null && moc._modelCount === 0, "Moc 内存或引用数未清理");
      check(internal.destroyed && internal.coreModel === undefined, "内部模型未进入已销毁状态");
      samples.push({ ...sample, releases, remainingModelCount: moc._modelCount });
    }
  }
  app.destroy(true, true);
  console.log(reportPrefix + JSON.stringify({ engine: engine.VERSION, cycles: 5, samples }));
}

verifyLifecycle().catch(error => {
  console.log(reportPrefix + JSON.stringify({ failure: error.stack ?? String(error), samples }));
});
