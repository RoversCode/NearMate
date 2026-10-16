const loadedScriptUrls = new Set<string>();
const loadingScriptTasks = new Map<string, Promise<void>>();

/**
 * loadScriptOnce 负责把项目内复制的浏览器运行时脚本按需注入页面。
 * 核心入参是脚本 URL，输出为脚本完成加载后的 Promise。
 * 边界异常：如果脚本下载或执行失败，会移除失败缓存并向调用方抛出错误，便于后续重试或展示失败状态。
 */
export function loadScriptOnce(scriptUrl: string): Promise<void> {
  /**
   * 已完成加载的脚本直接复用，避免 React 严格模式或多个舞台实例重复注入同一份 runtime。
   */
  if (loadedScriptUrls.has(scriptUrl)) {
    return Promise.resolve();
  }

  const activeTask = loadingScriptTasks.get(scriptUrl);

  /**
   * 如果同一个脚本正在加载，后续调用共享同一个 Promise。
   * 这样可以避免并发创建多个 script 标签，引发 runtime 重复初始化。
   */
  if (activeTask) {
    return activeTask;
  }

  const task = new Promise<void>((resolve, reject) => {
    /**
     * script 标签只负责加载项目内 public 资源，不直接承载业务状态。
     * 这里使用 async=false 保持同类 runtime 的执行顺序更可控。
     */
    const scriptElement = document.createElement("script");
    scriptElement.src = scriptUrl;
    scriptElement.async = false;

    scriptElement.addEventListener(
      "load",
      () => {
        loadedScriptUrls.add(scriptUrl);
        loadingScriptTasks.delete(scriptUrl);
        resolve();
      },
      { once: true }
    );

    scriptElement.addEventListener(
      "error",
      () => {
        loadingScriptTasks.delete(scriptUrl);
        scriptElement.remove();
        reject(new Error(`无法加载脚本：${scriptUrl}`));
      },
      { once: true }
    );

    document.head.appendChild(scriptElement);
  });

  loadingScriptTasks.set(scriptUrl, task);

  return task;
}
