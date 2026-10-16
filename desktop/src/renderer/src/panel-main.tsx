import React from "react";
import ReactDOM from "react-dom/client";
import { PanelApp } from "./panel/PanelApp";
import "./styles.css";

/**
 * panel renderer 入口同样只承担挂载职责。
 * 这里不接入复杂状态与业务逻辑，确保当前任务聚焦在构建入口拆分本身。
 */
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <PanelApp />
  </React.StrictMode>
);
