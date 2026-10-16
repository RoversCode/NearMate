import { execFile as execFileCallback } from "node:child_process";
import { readFileSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const execFile = promisify(execFileCallback);
const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_CONTRACT_PATH = path.join(REPOSITORY_ROOT, "shared", "dev-ports.json");
const SERVICE_NAMES = ["backend", "console", "desktop", "livekit"];

/**
 * 读取并校验开发服务端口契约。
 *
 * Args:
 *   contractPath: 端口契约文件路径；默认使用仓库内的 shared/dev-ports.json。
 *
 * Returns:
 *   包含四个服务主机和端口的只读对象。
 *
 * Raises:
 *   Error: 文件不是对象、服务缺失、主机为空或端口不在有效范围时抛出。
 */
export function readDevPorts(contractPath = DEFAULT_CONTRACT_PATH) {
  let payload;

  try {
    payload = JSON.parse(readFileSync(contractPath, "utf8"));
  } catch (error) {
    throw new Error(`无法读取开发端口契约：${contractPath}；${error.message}`);
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("开发端口契约必须是对象。");
  }

  const unexpectedServices = Object.keys(payload).filter(serviceName => !SERVICE_NAMES.includes(serviceName));
  if (unexpectedServices.length > 0) {
    throw new Error(`开发端口契约包含未知服务：${unexpectedServices.join(", ")}。`);
  }

  const ports = {};

  for (const serviceName of SERVICE_NAMES) {
    const service = payload[serviceName];
    const port = service?.port;
    const host = service?.host;

    if (!service || typeof service !== "object" || Array.isArray(service)) {
      throw new Error(`开发端口契约缺少服务：${serviceName}。`);
    }
    if (typeof host !== "string" || host.trim() === "") {
      throw new Error(`开发端口契约中的 ${serviceName}.host 必须是非空字符串。`);
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error(`开发端口契约中的 ${serviceName}.port 必须是 1 到 65535 的整数。`);
    }

    ports[serviceName] = Object.freeze({ host: host.trim(), port });
  }

  const occupiedContractPorts = new Map();
  for (const [serviceName, endpoint] of Object.entries(ports)) {
    const key = `${endpoint.host}:${endpoint.port}`;
    const previousServiceName = occupiedContractPorts.get(key);
    if (previousServiceName) {
      throw new Error(`开发端口契约中的 ${previousServiceName} 和 ${serviceName} 使用了同一地址：${key}。`);
    }
    occupiedContractPorts.set(key, serviceName);
  }

  return Object.freeze(ports);
}

/**
 * 用临时监听器检查端口是否能被当前服务绑定。
 *
 * Args:
 *   endpoint: 要检查的主机和端口。
 *
 * Returns:
 *   端口空闲时返回 true。
 *
 * Raises:
 *   Error: 操作系统返回无法归类的绑定错误时抛出。
 */
export function probePort(endpoint) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    let settled = false;

    const closeServer = () => {
      if (server.listening) {
        server.close(() => resolve(true));
        return;
      }
      resolve(true);
    };

    server.once("error", error => {
      if (settled) {
        return;
      }
      settled = true;
      if (error.code === "EADDRINUSE") {
        reject(error);
        return;
      }
      reject(error);
    });
    server.once("listening", () => {
      if (settled) {
        return;
      }
      settled = true;
      closeServer();
    });
    server.listen(endpoint.port, endpoint.host);
  });
}

/**
 * 从 Windows 的 netstat 输出中找出监听指定端口的 PID。
 *
 * Args:
 *   output: netstat -ano -p tcp 的原始输出。
 *   endpoint: 要匹配的主机和端口。
 *
 * Returns:
 *   找到的第一个监听 PID；无法解析时返回 null。
 */
export function parseListeningPid(output, endpoint) {
  for (const line of output.split(/\r?\n/)) {
    const columns = line.trim().split(/\s+/);
    if (columns.length < 5 || columns[0].toUpperCase() !== "TCP") {
      continue;
    }
    if (columns[3].toUpperCase() !== "LISTENING") {
      continue;
    }
    const localPort = parseEndpointPort(columns[1]);
    if (localPort !== endpoint.port) {
      continue;
    }
    if (!matchesLocalHost(columns[1], endpoint.host)) {
      continue;
    }
    const pid = Number.parseInt(columns[4], 10);
    if (Number.isInteger(pid) && pid > 0) {
      return pid;
    }
  }
  return null;
}

/**
 * 查询端口占用者的 PID 和进程名，不读取命令行参数。
 *
 * Args:
 *   endpoint: 要查询的主机和端口。
 *
 * Returns:
 *   包含 pid 和 processName 的诊断信息；查询不可用时降级为 null。
 */
async function findPortOwner(endpoint) {
  if (process.platform !== "win32") {
    return { pid: null, processName: null };
  }

  try {
    const { stdout } = await execFile("netstat", ["-ano", "-p", "tcp"], { windowsHide: true });
    const pid = parseListeningPid(stdout, endpoint);
    if (!pid) {
      return { pid: null, processName: null };
    }

    try {
      const task = await execFile("tasklist", ["/FI", `PID eq ${pid}`, "/FO", "CSV", "/NH"], { windowsHide: true });
      const match = /^\s*"([^"]+)"/.exec(task.stdout.trim());
      return { pid, processName: match?.[1] ?? null };
    } catch {
      return { pid, processName: null };
    }
  } catch {
    return { pid: null, processName: null };
  }
}

/**
 * 检查一个开发服务端口并附带占用进程诊断。
 *
 * Args:
 *   serviceName: 用于错误提示的服务名称。
 *   endpoint: 服务主机和端口。
 *   dependencies: 可注入探针和进程查询，便于单元测试。
 *
 * Returns:
 *   空闲时返回 ok=true；冲突时返回 ok=false 和占用者信息。
 *
 * Raises:
 *   Error: 端口探针出现非占用类系统错误时抛出。
 */
export async function checkPort(serviceName, endpoint, dependencies = {}) {
  const probe = dependencies.probe ?? probePort;
  const lookupOwner = dependencies.lookupOwner ?? findPortOwner;

  try {
    await probe(endpoint);
    return { serviceName, endpoint, ok: true, owner: null };
  } catch (error) {
    if (error.code !== "EADDRINUSE") {
      throw new Error(`检查 ${serviceName} 端口 ${endpoint.host}:${endpoint.port} 失败：${error.message}`);
    }
    return { serviceName, endpoint, ok: false, owner: await lookupOwner(endpoint) };
  }
}

/**
 * 检查一组开发服务端口。
 *
 * Args:
 *   ports: readDevPorts 返回的服务端点对象。
 *   serviceNames: 要检查的服务名；独立命令可传入单个服务。
 *   dependencies: 可注入端口检查依赖。
 *
 * Returns:
 *   包含全部检查结果和冲突结果的报告。
 */
export async function checkDevelopmentPorts(ports, serviceNames = SERVICE_NAMES, dependencies = {}) {
  const results = [];
  for (const serviceName of serviceNames) {
    const endpoint = ports[serviceName];
    if (!endpoint) {
      throw new Error(`未知开发服务：${serviceName}。`);
    }
    results.push(await checkPort(serviceName, endpoint, dependencies));
  }
  return { results, conflicts: results.filter(result => !result.ok), ok: results.every(result => result.ok) };
}

/**
 * 格式化端口冲突诊断，不输出占用者完整命令行。
 *
 * Args:
 *   conflict: checkPort 返回的冲突结果。
 *
 * Returns:
 *   可直接显示给开发者的中文诊断文本。
 */
export function formatPortConflict(conflict) {
  const { serviceName, endpoint, owner } = conflict;
  const pid = owner?.pid ?? "未知";
  const processName = owner?.processName ?? "未知进程";
  return [
    `[端口冲突] ${serviceName} 需要 ${endpoint.host}:${endpoint.port}，但该端口已被占用。`,
    `占用者：PID ${pid}，进程 ${processName}。`,
    "请关闭占用该端口的开发服务后重试；本工具不会自动结束未知进程。",
  ].join("\n");
}

function parseEndpointPort(value) {
  const bracketedMatch = /^\[[^\]]+\]:(\d+)$/.exec(value);
  if (bracketedMatch) {
    return Number.parseInt(bracketedMatch[1], 10);
  }
  const separator = value.lastIndexOf(":");
  return separator >= 0 ? Number.parseInt(value.slice(separator + 1), 10) : null;
}

function matchesLocalHost(value, expectedHost) {
  const endpointHost = value.startsWith("[") ? value.slice(1, value.indexOf("]")) : value.slice(0, value.lastIndexOf(":"));
  return endpointHost === expectedHost || endpointHost === "0.0.0.0" || endpointHost === "[::]" || endpointHost === "::";
}

/**
 * 作为独立命令检查一个或全部开发服务端口。
 *
 * Args:
 *   argv: 命令行参数，可传服务名；未传时检查全部服务。
 *
 * Returns:
 *   进程退出码。
 */
async function runPortCheckCli(argv = process.argv.slice(2)) {
  const requestedServices = argv.length > 0 ? argv : SERVICE_NAMES;
  const ports = readDevPorts();
  const report = await checkDevelopmentPorts(ports, requestedServices);
  if (report.ok) {
    for (const result of report.results) {
      console.log(`[端口检查通过] ${result.serviceName} ${result.endpoint.host}:${result.endpoint.port}`);
    }
    return 0;
  }
  for (const conflict of report.conflicts) {
    console.error(formatPortConflict(conflict));
  }
  return 1;
}

const isMainModule = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMainModule) {
  runPortCheckCli().then(code => {
    process.exitCode = code;
  }).catch(error => {
    console.error(`[端口检查失败] ${error.message}`);
    process.exitCode = 1;
  });
}
