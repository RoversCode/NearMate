/**
 * SidecarChild 是 supervisor 真实需要的子进程窄接口。
 * 这样生产环境可以使用 Node ChildProcess，测试也能注入轻量 fake。
 */
export type SidecarChild = {
  stdout?: {
    on(eventName: "data", listener: (chunk: Buffer | string) => void): unknown;
  } | null;
  stderr?: {
    on(eventName: "data", listener: (chunk: Buffer | string) => void): unknown;
  } | null;
  kill(): unknown;
  on(eventName: "error", listener: (error: unknown) => void): unknown;
  on(eventName: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  on(eventName: "spawn", listener: () => void): unknown;
};

/**
 * SpawnSidecarProcess 是启动 Python sidecar 的可注入边界。
 * 测试通过替换它验证参数，不真的启动 Python。
 */
export type SpawnSidecarProcess = (
  command: string,
  args: string[],
  options: {
    cwd: string;
    env: NodeJS.ProcessEnv;
    stdio: ["ignore", "pipe", "pipe"];
    windowsHide: boolean;
  }
) => SidecarChild;

/**
 * stopChildForReplacement 在替换 sidecar 前终止旧进程并等待它退出。
 *
 * @param child 当前 sidecar 子进程。
 * @param timeoutMs 最长等待时间。
 */
export async function stopChildForReplacement(
  child: SidecarChild,
  timeoutMs: number
): Promise<void> {
  /**
   * 先挂 exit 监听再 kill，避免旧进程同步退出时错过释放设备的信号。
   */
  const terminationPromise = new Promise<void>((resolve, reject) => {
    child.on("exit", () => {
      resolve();
    });
    child.on("error", error => {
      // error 可能表示 kill 失败，不能把“失去句柄”误当成旧进程已经退出。
      reject(error);
    });
  });

  try {
    child.kill();
  } catch (error) {
    // 无法发出停止请求时不能再启动下一代，否则旧进程可能继续占用麦克风和 LiveKit 身份。
    throw new Error("AEC sidecar 停止请求失败，拒绝启动下一代进程", { cause: error });
  }

  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`AEC sidecar 未能在 ${Math.max(1, timeoutMs)}ms 内退出，拒绝启动下一代进程`));
    }, Math.max(1, timeoutMs));

    terminationPromise.then(
      () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve();
      },
      error => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(new Error("AEC sidecar 停止请求未确认旧进程退出", { cause: error }));
      }
    );
  });
}
