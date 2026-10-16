import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureLiveKitServerExecutable } from "./livekit-server-provision.mjs";
import { checkPort, formatPortConflict, readDevPorts } from "./dev-port-check.mjs";
import { createLiveKitLogFilter } from "./livekit-log-filter.mjs";

const DEV_PORTS = readDevPorts();
// 默认 LiveKit 地址来自共享开发端口契约，避免脚本和 Backend 各写一份端口。
const DEFAULT_LIVEKIT_URL = `ws://${DEV_PORTS.livekit.host}:${DEV_PORTS.livekit.port}`;
// 读取本地开发使用的 LiveKit 鉴权配置；未显式注入时使用约定的开发默认值，方便新环境直接启动。
const apiKey = process.env.LIVEKIT_API_KEY ?? "devkey";
const apiSecret = process.env.LIVEKIT_API_SECRET ?? "devsecret";

// 现在改为只读取 LIVEKIT_URL，业务意图是让 README、后端配置和本地启动命令共享同一个地址契约。
// 读取本地 LiveKit 访问地址；该值是 README、后端配置和本地启动命令之间的单一事实来源。
const liveKitUrl = process.env.LIVEKIT_URL ?? DEFAULT_LIVEKIT_URL;

/**
 * parseLiveKitUrl 解析本地 LiveKit 开发地址，并转换成 livekit-server 可接受的监听主机与端口。
 * 核心入参是 LIVEKIT_URL 字符串；预期输出包含原始 URL、hostname、port 与 bindHost。
 * 边界异常：URL 格式非法、协议非 ws/wss、包含非根路径、缺少 hostname 或缺少显式端口时会输出中文错误并返回 null，避免服务静默绑定到错误端口。
 */
function parseLiveKitUrl(value) {
  let parsedUrl;

  // 使用标准 URL 解析器校验格式，业务意图是让 README、后端和脚本共享同一种 URL 写法。
  try {
    parsedUrl = new URL(value);
  } catch {
    console.error(`[livekit:dev] LIVEKIT_URL 格式无效：${value}`);
    console.error(`[livekit:dev] 请使用包含端口的 WebSocket 地址，例如 ${DEFAULT_LIVEKIT_URL}。`);
    return null;
  }

  // 只接受 WebSocket 协议，业务意图是避免开发者误把后端 HTTP 地址或浏览器页面地址配置成 LiveKit 信令地址。
  if (parsedUrl.protocol !== "ws:" && parsedUrl.protocol !== "wss:") {
    console.error(`[livekit:dev] LIVEKIT_URL 协议无效：${value}`);
    console.error(`[livekit:dev] 请使用类似 ${DEFAULT_LIVEKIT_URL} 的根地址，只允许 ws:// 或 wss://。`);
    return null;
  }

  // LiveKit 本地启动命令只从根地址推导监听端口，业务意图是拒绝带路径、查询串或片段的页面式地址。
  if ((parsedUrl.pathname && parsedUrl.pathname !== "/") || parsedUrl.search || parsedUrl.hash) {
    console.error(`[livekit:dev] LIVEKIT_URL 必须是 LiveKit 根地址，不能包含路径、查询串或片段：${value}`);
    console.error(`[livekit:dev] 请使用类似 ${DEFAULT_LIVEKIT_URL} 的根地址。`);
    return null;
  }

  // 本地开发的 bind 需要明确主机名，否则 livekit-server 可能无法按预期监听本机地址。
  if (!parsedUrl.hostname) {
    console.error(`[livekit:dev] LIVEKIT_URL 缺少主机名：${value}`);
    console.error(`[livekit:dev] 请使用包含主机名和端口的地址，例如 ${DEFAULT_LIVEKIT_URL}。`);
    return null;
  }

  // 强制要求显式端口，业务意图是让本地 LiveKit 地址和实际监听端口一一对应，避免 ws/wss 默认端口带来误判。
  if (!parsedUrl.port) {
    console.error(`[livekit:dev] LIVEKIT_URL 必须显式包含端口：${value}`);
    console.error(`[livekit:dev] 请改为类似 ${DEFAULT_LIVEKIT_URL} 的地址。`);
    return null;
  }

  return {
    displayUrl: parsedUrl.toString(),
    hostname: parsedUrl.hostname,
    port: parsedUrl.port,
    // 关键变量 bindHost 表示传给 livekit-server --bind 的主机部分；LiveKit 1.12.0 的 --bind 只接受 IP，不接受 host:port。
    bindHost: parsedUrl.hostname,
  };
}

/**
 * startLiveKitServer 启动自托管 LiveKit 开发实例。
 * 核心入参是已解析的可执行文件路径、监听主机与端口；预期输出是已挂接退出处理的子进程。
 * 边界异常：spawn 失败时会输出中文提示并退出 1；子进程正常或异常结束时透传其退出码。
 */
export function startLiveKitServer(executablePath, bindHost, port) {
  // 所有平台都禁用 shell；Windows 会由 provisioning 模块优先复用 PATH 或项目内缓存，避免密钥进入 shell 解析层。
  const child = spawn(
    executablePath,
    [
      "--dev",
      "--bind",
      bindHost,
      "--port",
      port,
      "--keys",
      `${apiKey}: ${apiSecret}`,
    ],
    {
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
    },
  );

  // 两条输出流共用一次提示状态，避免 stdout/stderr 分流时重复报告同一条已知平台限制。
  let knownWindowsCpuWarningReported = false;
  const forwardOutput = (stream, target) => {
    const filter = createLiveKitLogFilter({
      platform: process.platform,
      write: text => target.write(text),
      onKnownWindowsCpuWarning: () => {
        if (knownWindowsCpuWarningReported) {
          return;
        }
        knownWindowsCpuWarningReported = true;
        process.stderr.write(
          "[livekit:dev] Windows CPU monitoring unavailable; capacity management disabled for local development.\n",
        );
      },
    });

    stream?.on("data", chunk => filter.push(chunk));
    stream?.on("end", () => filter.flush());
  };

  forwardOutput(child.stdout, process.stdout);
  forwardOutput(child.stderr, process.stderr);

  // 捕获可执行文件启动失败；业务意图是把 Node 的底层 ENOENT 转成开发者能直接处理的安装指引。
  child.on("error", error => {
    if (error.code === "ENOENT") {
      console.error(
        "[livekit:dev] 未找到 livekit-server 可执行文件。Windows 会尝试自动下载；其他平台请先安装 livekit-server。",
      );
    } else {
      console.error(`[livekit:dev] LiveKit Server 启动失败：${error.message}`);
    }

    process.exit(1);
  });

  // 将子进程退出状态透传给调用方；业务意图是区分正常退出码和系统信号，避免信号终止被误判为成功。
  child.on("close", (code, signal) => {
    if (signal) {
      console.error(`[livekit:dev] LiveKit Server 被信号 ${signal} 终止。`);
      process.exit(1);
      return;
    }

    process.exit(code ?? 0);
  });
}

/**
 * main 是本地 LiveKit 开发命令入口。
 * 核心入参来自环境变量；预期输出是启动一个与 LIVEKIT_URL、LIVEKIT_API_KEY、LIVEKIT_API_SECRET 对齐的本地服务。
 * 边界异常：配置非法或 Windows 可执行文件解析失败时直接退出 1，避免进入半启动状态。
 */
export async function main({ liveKitUrlValue = liveKitUrl, checkPortImpl = checkPort, ensureExecutable = ensureLiveKitServerExecutable } = {}) {
  const parsedLiveKitUrl = parseLiveKitUrl(liveKitUrlValue);

  if (!parsedLiveKitUrl) {
    return 1;
  }

  const portCheck = await checkPortImpl("livekit", {
    host: parsedLiveKitUrl.bindHost,
    port: Number(parsedLiveKitUrl.port),
  });
  if (!portCheck.ok) {
    console.error(formatPortConflict(portCheck));
    return 1;
  }

  const executablePath = await ensureExecutable();

  if (!executablePath) {
    return 1;
  }

  console.log(`Starting local LiveKit Server on ${parsedLiveKitUrl.displayUrl}`);
  console.log(`Binding LiveKit Server to ${parsedLiveKitUrl.hostname}:${parsedLiveKitUrl.port}`);
  console.log("Using LIVEKIT_API_KEY from env or devkey fallback for local development.");
  console.log("Using LIVEKIT_API_SECRET from env or devsecret fallback for local development.");

  startLiveKitServer(executablePath, parsedLiveKitUrl.bindHost, parsedLiveKitUrl.port);
  return 0;
}

const isMainModule = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMainModule) {
  main().then(code => {
    process.exitCode = code;
  }).catch(error => {
    console.error(`[livekit:dev] 启动失败：${error.message}`);
    process.exitCode = 1;
  });
}
