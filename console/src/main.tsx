import React from "react";
import ReactDOM from "react-dom/client";

import { App } from "./App";
import { applyNearMateConsoleBranding } from "./brand/brandIdentity";
import "./styles/index.css";

// 在 React 首次渲染前统一设置标题和 favicon，避免首屏短暂显示历史产品名。
applyNearMateConsoleBranding();

// 控制台只挂载到独立 root 节点，避免影响后端同域下的其他静态页面或调试入口。
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
