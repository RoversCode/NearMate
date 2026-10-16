import assert from "node:assert/strict";
import net from "node:net";
import test from "node:test";
import { checkDevelopmentPorts, checkPort, formatPortConflict, parseListeningPid, probePort, readDevPorts } from "./dev-port-check.mjs";

test("共享开发端口契约包含四个唯一的有效端点", () => {
  const ports = readDevPorts();
  assert.deepEqual(ports.backend, { host: "127.0.0.1", port: 8765 });
  assert.deepEqual(ports.console, { host: "127.0.0.1", port: 5173 });
  assert.deepEqual(ports.desktop, { host: "127.0.0.1", port: 5174 });
  assert.deepEqual(ports.livekit, { host: "127.0.0.1", port: 7880 });
});

test("端口检查支持注入依赖，并只返回 PID/进程名诊断", async () => {
  const endpoint = { host: "127.0.0.1", port: 5173 };
  const report = await checkPort("console", endpoint, {
    probe: async () => {
      const error = new Error("占用");
      error.code = "EADDRINUSE";
      throw error;
    },
    lookupOwner: async () => ({ pid: 42, processName: "node.exe", commandLine: "--secret=should-not-appear" })
  });

  assert.equal(report.ok, false);
  const message = formatPortConflict(report);
  assert.match(message, /console/);
  assert.match(message, /5173/);
  assert.match(message, /42/);
  assert.match(message, /node\.exe/);
  assert.doesNotMatch(message, /secret/);
  assert.doesNotMatch(message, /commandLine/);
});

test("真实占用端口会被 bind 探针识别", async t => {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => server.close());
  const address = server.address();
  assert.equal(typeof address, "object");

  const report = await checkPort("test", { host: "127.0.0.1", port: address.port }, {
    lookupOwner: async () => ({ pid: null, processName: null })
  });
  assert.equal(report.ok, false);
});

test("netstat 输出只提取监听状态的 PID", () => {
  const output = [
    "  TCP    127.0.0.1:5173    0.0.0.0:0    LISTENING    1234",
    "  TCP    127.0.0.1:5173    0.0.0.0:0    ESTABLISHED  5678",
  ].join("\n");
  assert.equal(parseListeningPid(output, { host: "127.0.0.1", port: 5173 }), 1234);
});

test("整组端口预检返回所有冲突，不短路其它服务", async () => {
  const ports = {
    backend: { host: "127.0.0.1", port: 8765 },
    console: { host: "127.0.0.1", port: 5173 },
    desktop: { host: "127.0.0.1", port: 5174 },
    livekit: { host: "127.0.0.1", port: 7880 }
  };
  const report = await checkDevelopmentPorts(ports, ["backend", "console", "desktop", "livekit"], {
    probe: async endpoint => {
      if (endpoint.port === 5173 || endpoint.port === 7880) {
        const error = new Error("占用");
        error.code = "EADDRINUSE";
        throw error;
      }
    },
    lookupOwner: async () => ({ pid: null, processName: null })
  });
  assert.deepEqual(report.conflicts.map(conflict => conflict.serviceName), ["console", "livekit"]);
});

test("空闲端口探针可以成功关闭临时监听器", async () => {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  server.close();
  await new Promise(resolve => server.once("close", resolve));
  await assert.doesNotReject(() => probePort({ host: "127.0.0.1", port: address.port }));
});
