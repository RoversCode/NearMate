import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "vite";

const require = createRequire(import.meta.url);
const scriptPath = fileURLToPath(import.meta.url);
const workspace = path.resolve(path.dirname(scriptPath), "../..");
const rendererPublic = path.join(workspace, "desktop/src/renderer/public");
const reportPrefix = "LIVE2D_LIFECYCLE_REPORT ";

/**
 * 构建真实 Electron 回归页，使用本机已安装的引擎和项目内 Cubism Core。
 *
 * Args:
 *   无；--suite integration 运行完整形象回归，--speech 可指定本地语音 WAV。
 *   --model 可重复指定生命周期模型，--engine-entry 用于比较待升级版本。
 *
 * Returns:
 *   子进程结束后完成；验证失败时设置非零退出码。
 */
async function launchVerification() {
  const models = [];
  let engineEntry;
  let speech;
  let suite = "lifecycle";
  for (let index = 2; index < process.argv.length; index += 2) {
    const flag = process.argv[index];
    const value = process.argv[index + 1];
    if (!value || !["--model", "--engine-entry", "--suite", "--speech"].includes(flag)) {
      throw new Error("用法：node desktop/scripts/verify-live2d-lifecycle.mjs [--suite lifecycle|integration] [--speech speech.wav] [--model model3.json] [--engine-entry cubism.es.js]");
    }
    if (flag === "--model") models.push(path.resolve(value));
    else if (flag === "--engine-entry") engineEntry = path.resolve(value);
    else if (flag === "--speech") speech = path.resolve(value);
    else suite = value;
  }
  if (!["lifecycle", "integration"].includes(suite)) throw new Error("未知验证套件");
  if (speech) await fs.access(speech);
  if (!models.length) models.push(path.join(rendererPublic, "live2d/resources/Haru/Haru.model3.json"));
  for (const model of models) await fs.access(model);
  const output = await fs.mkdtemp(path.join(os.tmpdir(), "nearmate-live2d-lifecycle-"));
  const config = { output, models, suite, speech };
  await fs.writeFile(path.join(output, "config.json"), JSON.stringify(config));
  await build({
    configFile: false,
    root: workspace,
    resolve: { alias: engineEntry ? { "untitled-pixi-live2d-engine/cubism": engineEntry } : {} },
    define: { "process.env.NODE_ENV": '"production"' },
    logLevel: "error",
    build: {
      outDir: output, emptyOutDir: false, minify: false,
      lib: {
        entry: path.join(path.dirname(scriptPath), suite === "integration"
          ? "live2d-integration-scenario.mjs" : "live2d-lifecycle-renderer.mjs"),
        formats: ["es"],
        fileName: () => "renderer.js"
      },
      // 保留动态模块边界，Cubism 引擎必须等 Core 脚本就绪后才能求值。
      rollupOptions: { output: { inlineDynamicImports: false } }
    }
  });
  await fs.writeFile(path.join(output, "index.html"), `<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>Live2D 生命周期回归</title>
<body style="margin:0;overflow:hidden"><script type="module" src="/renderer.js"></script></body></html>`);
  const environment = { ...process.env, NEARMATE_LIFECYCLE_CONFIG: path.join(output, "config.json") };
  delete environment.ELECTRON_RUN_AS_NODE;
  // Electron 33 的默认启动器通过 CommonJS 接收入口，再显式加载本脚本的 ESM 主体。
  const electronEntry = path.join(output, "electron-main.cjs");
  await fs.writeFile(electronEntry, `import(${JSON.stringify(pathToFileURL(scriptPath).href)}).catch(error => {
    console.error(error); require("electron").app.exit(1);
  });`);
  console.log(`验证产物目录：${output}`);
  const child = spawn(require("electron"), [electronEntry], {
    env: environment, stdio: "inherit", windowsHide: true
  });
  process.exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", code => resolve(code ?? 1));
  });
}

/**
 * 在隔离的隐藏窗口中运行真实模型加载、绘制和销毁，并保存截图及失败证据。
 *
 * Args:
 *   无；配置路径通过父进程专用环境变量传入。
 *
 * Returns:
 *   无；验证报告到达或超时后关闭当前验证进程。
 */
async function runElectronVerification() {
  const { app, BrowserWindow } = require("electron");
  const config = JSON.parse(await fs.readFile(process.env.NEARMATE_LIFECYCLE_CONFIG, "utf8"));
  app.setPath("userData", path.join(config.output, "electron-profile"));
  app.commandLine.appendSwitch("disable-renderer-backgrounding");
  app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
  await app.whenReady();
  const window = new BrowserWindow({
    show: false, width: 720, height: 720,
    webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false, backgroundThrottling: false }
  });
  window.webContents.setAudioMuted(true);
  window.webContents.setFrameRate(60);
  const errors = [];
  let finished = false;
  const timeout = setTimeout(() => finish({ failure: "真实模型验证超过 180 秒" }), 180_000);

  /** 保存唯一终态，确保网络异常或渲染崩溃也会留下可检查的失败记录。 */
  async function finish(report) {
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    const result = { ...report, rendererErrors: errors };
    await fs.writeFile(path.join(config.output, "report.json"), JSON.stringify(result, null, 2));
    console.log(reportPrefix + JSON.stringify(result));
    server.close();
    app.exit(report.failure || errors.length ? 1 : 0);
  }
  window.webContents.on("console-message", (_event, level, message) => {
    if (message.startsWith(reportPrefix)) {
      void finish(JSON.parse(message.slice(reportPrefix.length)));
    } else if (message.startsWith("LIVE2D_PROGRESS ")) {
      console.log(message);
    } else if (level >= 3) {
      errors.push(message);
    }
  });
  window.webContents.on("render-process-gone", (_event, details) => void finish({ failure: details.reason }));
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://127.0.0.1");
      const pathname = decodeURIComponent(url.pathname);
      if (pathname === "/verification.json") {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify({ speech: !!config.speech }));
        return;
      }
      if (pathname === "/speech.wav" && config.speech) {
        response.setHeader("Content-Type", "audio/wav");
        response.end(await fs.readFile(config.speech));
        return;
      }
      if (pathname === "/models.json") {
        response.setHeader("Content-Type", "application/json");
        response.end(JSON.stringify(config.models.map((model, index) => `/models/${index}/${encodeURIComponent(path.basename(model))}`)));
        return;
      }
      if (/^\/capture\/[a-z0-9-]+$/.test(pathname)) {
        const width = Number(url.searchParams.get("width")) || 720;
        const height = Number(url.searchParams.get("height")) || 720;
        if (width < 1 || width > 720 || height < 1 || height > 720) throw new Error("截图尺寸不合法");
        const snapshot = await window.webContents.capturePage({ x: 0, y: 0, width, height });
        await fs.writeFile(path.join(config.output, `model-${pathname.split("/").at(-1)}.png`), snapshot.toPNG());
        response.end("ok");
        return;
      }
      const modelMatch = /^\/models\/(\d+)\/(.+)$/.exec(pathname);
      let root = config.output;
      let relative = pathname === "/" ? "index.html" : pathname.slice(1);
      if (modelMatch && config.models[Number(modelMatch[1])]) {
        root = path.dirname(config.models[Number(modelMatch[1])]);
        relative = modelMatch[2];
      } else if (pathname.startsWith("/live2d/")) {
        root = rendererPublic;
      }
      const file = path.resolve(root, relative);
      // 测试服务只公开明确指定的模型目录与生成目录，不允许相对路径越界。
      if (!file.startsWith(root + path.sep)) throw new Error("资源路径越界");
      const content = await fs.readFile(file);
      const mime = { ".js": "text/javascript", ".mjs": "text/javascript", ".html": "text/html", ".json": "application/json", ".png": "image/png" };
      response.setHeader("Content-Type", mime[path.extname(file)] ?? "application/octet-stream");
      response.end(content);
    } catch (error) {
      response.statusCode = 404;
      response.end(String(error));
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  await window.loadURL(`http://127.0.0.1:${server.address().port}/`);
}

try {
  if (process.versions.electron) await runElectronVerification();
  else await launchVerification();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
