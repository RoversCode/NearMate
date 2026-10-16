import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { buildSpawnSpec } from "./dev-process.mjs";
import { checkDevelopmentPorts, formatPortConflict, readDevPorts } from "./dev-port-check.mjs";

const PNPM_COMMAND = process.platform === "win32" ? "pnpm.cmd" : "pnpm";

/**
 * 为 Console 或 Desktop 的独立开发命令执行单服务预检，再转交真实启动命令。
 *
 * Args:
 *   argv: 第一个参数是共享端口契约中的服务名，后面是要执行的命令及参数。
 *
 * Returns:
 *   被代理子进程的退出码。
 */
async function main(argv = process.argv.slice(2)) {
  const [serviceName, ...rawCommand] = argv;
  const command = rawCommand[0] === "--" ? rawCommand.slice(1) : rawCommand;

  if (!serviceName || command.length === 0) {
    console.error("用法：node scripts/independent-dev.mjs <service> -- <command> [args...]。");
    return 1;
  }

  const ports = readDevPorts();
  const report = await checkDevelopmentPorts(ports, [serviceName]);
  if (!report.ok) {
    for (const conflict of report.conflicts) {
      console.error(formatPortConflict(conflict));
    }
    return 1;
  }

  const executable = command[0] === "pnpm" ? PNPM_COMMAND : command[0];
  const spawnSpec = buildSpawnSpec(executable, command.slice(1));
  let child;
  try {
    child = spawn(spawnSpec.executable, spawnSpec.args, {
      cwd: process.cwd(),
      env: process.env,
      stdio: "inherit",
      windowsHide: true,
      shell: spawnSpec.shell,
    });
  } catch (error) {
    console.error(`[${serviceName}] 独立开发命令启动失败：${error.message}`);
    return 1;
  }

  // 独立命令也要负责自己的子进程收口，避免 Ctrl+C 后留下仍占用开发端口的 Vite 进程。
  let isStopping = false;
  const stopChild = () => {
    if (isStopping || !child.pid || child.killed) {
      return;
    }
    isStopping = true;
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      return;
    }
    child.kill("SIGTERM");
  };

  process.on("SIGINT", stopChild);
  process.on("SIGTERM", stopChild);

  return await new Promise(resolve => {
    child.on("error", error => {
      process.removeListener("SIGINT", stopChild);
      process.removeListener("SIGTERM", stopChild);
      console.error(`[${serviceName}] 独立开发命令启动失败：${error.message}`);
      resolve(1);
    });
    child.on("exit", (code, signal) => {
      process.removeListener("SIGINT", stopChild);
      process.removeListener("SIGTERM", stopChild);
      if (signal) {
        resolve(1);
        return;
      }
      resolve(code ?? 0);
    });
  });
}

const isMainModule = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMainModule) {
  main().then(code => {
    process.exitCode = code;
  }).catch(error => {
    console.error(`[独立开发命令失败] ${error.message}`);
    process.exitCode = 1;
  });
}
