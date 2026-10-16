import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildSpawnSpec } from "./dev-process.mjs";
import { checkDevelopmentPorts, formatPortConflict, readDevPorts } from "./dev-port-check.mjs";

const PNPM_COMMAND = process.platform === "win32" ? "pnpm.cmd" : "pnpm";

const SERVICES = [
  {
    name: "livekit:dev",
    portServiceName: "livekit",
    args: ["livekit:dev"]
  },
  {
    name: "backend:core",
    portServiceName: "backend",
    args: ["backend:core"]
  },
  {
    name: "desktop:dev",
    portServiceName: "desktop",
    args: ["desktop:dev"]
  },
  {
    name: "console:dev",
    portServiceName: "console",
    args: ["console:dev"]
  }
];

/**
 * printUsage 输出根目录开发编排命令的使用说明。
 * 核心入参为空；预期输出是写入 stdout 的命令列表；边界异常是该函数不读取外部状态，因此不会因为服务配置缺失而失败。
 */
function printUsage() {
  console.log("Usage: pnpm dev [--dry-run]");
  console.log("");
  console.log("Starts local LiveKit, backend core, desktop dev, and console dev together.");
}

/**
 * printDryRun 输出即将启动的服务命令，但不真正创建子进程。
 * 核心入参为空；预期输出是每个服务的 pnpm 命令；边界异常是只读 SERVICES 常量，不触发任何长期运行进程。
 */
function printDryRun() {
  for (const service of SERVICES) {
    console.log(`[${service.name}] ${PNPM_COMMAND} ${service.args.join(" ")}`);
  }
}

/**
 * 在创建任何子进程前检查整个本地开发栈的端口。
 *
 * Returns:
 *   所有端口均可绑定时返回 true；出现冲突时输出诊断并返回 false。
 */
export async function preflightDevelopmentPorts({ ports = readDevPorts(), dependencies = {} } = {}) {
  const report = await checkDevelopmentPorts(
    ports,
    SERVICES.map(service => service.portServiceName),
    dependencies,
  );
  if (report.ok) {
    return true;
  }

  for (const conflict of report.conflicts) {
    console.error(`[dev] ${formatPortConflict(conflict)}`);
  }
  console.error("[dev] 端口预检失败，未启动任何开发服务。");
  return false;
}

/**
 * prefixOutput 将子进程输出按行加上服务名。
 * 核心入参是服务名、输出流与原始 chunk；预期输出是更容易区分来源的终端文本；边界异常是跨 chunk 半行会被当前实现即时输出，不影响开发日志可读性。
 */
function prefixOutput(serviceName, stream, chunk) {
  const lines = chunk.toString().split(/\r?\n/);

  for (const line of lines) {
    if (!line) {
      continue;
    }

    stream.write(`[${serviceName}] ${line}\n`);
  }
}

/**
 * stopProcessTree 负责跨平台终止一个服务进程及其子进程。
 * 核心入参是 ChildProcess 实例；预期输出是向操作系统发出终止信号；边界异常是进程已经退出或没有 pid 时直接返回。
 */
function stopProcessTree(child) {
  if (!child.pid || child.killed) {
    return;
  }

  /**
   * Windows 下 pnpm、uv、electron-vite 往往会再拉起子进程。
   * 直接 kill 父进程容易留下孤儿进程，因此用 taskkill 按进程树清理。
   */
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true
    });
    return;
  }

  child.kill("SIGTERM");
}

/**
 * startService 启动单个长期运行服务，并把 stdout/stderr 打上服务名前缀。
 * 核心入参是服务配置；预期输出是已启动的 ChildProcess；边界异常是 pnpm 不可执行时会触发 error 事件并交给主流程收口。
 */
function startService(service) {
  const spawnSpec = buildSpawnSpec(PNPM_COMMAND, service.args);
  const child = spawn(spawnSpec.executable, spawnSpec.args, {
    cwd: process.cwd(),
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
    shell: spawnSpec.shell
  });

  child.stdout?.on("data", chunk => {
    prefixOutput(service.name, process.stdout, chunk);
  });
  child.stderr?.on("data", chunk => {
    prefixOutput(service.name, process.stderr, chunk);
  });

  return child;
}

/**
 * main 是开发编排入口。
 * 核心入参来自 process.argv；预期输出是同时启动四个开发服务，并在任一服务退出或用户中断时清理全部子进程。
 * 边界异常：--help 只打印说明，--dry-run 只打印计划；真实启动时如果某个子进程异常退出，会停止整组服务并返回非零退出码。
 */
export async function main(argv = process.argv.slice(2), { ports, dependencies, startServiceImpl = startService } = {}) {
  const args = new Set(argv);

  if (args.has("--help") || args.has("-h")) {
    printUsage();
    return;
  }

  if (args.has("--dry-run")) {
    printDryRun();
    return;
  }

  if (!await preflightDevelopmentPorts({ ports, dependencies })) {
    process.exitCode = 1;
    return;
  }

  const children = SERVICES.map(startServiceImpl);
  let isShuttingDown = false;

  /**
   * shutdownAll 是整个开发栈唯一的退出收口点。
   * 业务意图是避免任一服务退出后其余服务继续占用端口，导致下一次启动出现假故障。
   */
  const shutdownAll = (exitCode = 0) => {
    if (isShuttingDown) {
      return;
    }

    isShuttingDown = true;
    process.exitCode = exitCode;

    for (const child of children) {
      stopProcessTree(child);
    }
  };

  for (const child of children) {
    child.on("error", error => {
      console.error(`[dev] failed to start service: ${error.message}`);
      shutdownAll(1);
    });

    child.on("exit", (code, signal) => {
      if (isShuttingDown) {
        return;
      }

      console.error(`[dev] service exited unexpectedly: code=${code ?? "null"} signal=${signal ?? "null"}`);
      shutdownAll(code && code > 0 ? code : 1);
    });
  }

  process.on("SIGINT", () => {
    shutdownAll(0);
  });
  process.on("SIGTERM", () => {
    shutdownAll(0);
  });
}

const isMainModule = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMainModule) {
  main().catch(error => {
    console.error(`[dev] 启动预检失败：${error.message}`);
    process.exitCode = 1;
  });
}
