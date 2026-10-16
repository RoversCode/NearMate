// 这个 Pixi 官方入口安装的是无 eval 同步实现，必须先于 Avatar 依赖图执行。
import "pixi.js/unsafe-eval";
import React from "react";
import ReactDOM from "react-dom/client";
import { AvatarApp } from "./avatar/AvatarApp";
import "./styles.css";

/**
 * avatar renderer 入口保持极薄，只负责挂载头像窗口根组件。
 * 这样多窗口构建链路一旦打通，后续 UI 演进就能局部推进而不污染入口职责。
 */
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <AvatarApp />
  </React.StrictMode>
);
