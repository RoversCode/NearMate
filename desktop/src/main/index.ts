import { bootstrap } from "./bootstrap";

/**
 * index.ts 退化为最薄启动垫片，避免旧入口继续承载窗口编排细节。
 */
void bootstrap().catch(error => {
  // bootstrap 已经向用户展示可操作的错误；这里收口 Promise，避免 Electron 静默吞掉失败原因。
  console.error("[companion-main] NearMate 启动未完成", error);
});
