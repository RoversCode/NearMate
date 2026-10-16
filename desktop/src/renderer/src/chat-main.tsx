import React from "react";
import ReactDOM from "react-dom/client";
import { ChatApp } from "./chat/ChatApp";
import "./styles.css";

/**
 * chat renderer 入口只负责挂载聊天窗口应用。
 * 聊天状态、控制链路与视图拆分到 chat 模块内，避免入口文件承载业务逻辑。
 */
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ChatApp />
  </React.StrictMode>
);
