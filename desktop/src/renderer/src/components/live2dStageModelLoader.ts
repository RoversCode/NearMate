import {
  assertLive2dModelSupported,
  loadCubism5Live2dEngine
} from "../lib/live2dRuntime";
import type { AvatarStageLive2dModel, PixiAvatarStageApp } from "./avatarStageTypes";

type LoadLive2dStageModelInput = {
  app: PixiAvatarStageApp;
  modelPath: string;
};

/**
 * loadLive2dStageModel 负责完成 Live2D 模型实例化前的资源加载事务。
 *
 * Args:
 *   input: 当前 Pixi app 与待加载的 model3.json 路径。
 *
 * Returns:
 *   已由 Live2D 引擎创建、但尚未提交到 Pixi stage 的模型实例。
 */
export async function loadLive2dStageModel({
  app,
  modelPath
}: LoadLive2dStageModelInput): Promise<AvatarStageLive2dModel> {
  /**
   * 在真正进入引擎加载前，先做一次 moc 版本兼容性预检。
   * 这样像 Ren 这类“目录存在但当前 core 不支持”的人物，可以直接给出明确错误，而不是只留下底层加载失败。
   */
  await assertLive2dModelSupported(modelPath);

  const { Live2DModel } = await loadCubism5Live2dEngine();

  return Live2DModel.from(modelPath, {
    ticker: app.ticker,
    textureOptions: { lod: "single-auto" }
  });
}
