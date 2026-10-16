import assert from "node:assert/strict";
import test from "node:test";
import { createLiveKitLogFilter, WINDOWS_CPU_WARNING_TEXT } from "./livekit-log-filter.mjs";

function collectFilterOutput(options = {}) {
  const output = [];
  let warningCount = 0;
  const filter = createLiveKitLogFilter({
    platform: "win32",
    write: text => output.push(text),
    onKnownWindowsCpuWarning: () => {
      warningCount += 1;
    },
    ...options,
  });

  return { filter, output, getWarningCount: () => warningCount };
}

test("LiveKit CPU 提示跨分块和堆栈过滤后只报告一次", () => {
  const { filter, output, getWarningCount } = collectFilterOutput();

  filter.push(`2026-08-16T21:48:18.807+0800 INFO starting\n`);
  filter.push(`2026-08-16T21:48:18.807+0800 ERROR ${WINDOWS_CPU_WARNING_TEXT.slice(0, 24)}`);
  filter.push(`${WINDOWS_CPU_WARNING_TEXT.slice(24)}\n`);
  filter.push("github.com/livekit/protocol/utils/hwstats.newPlatformCPUMonitor\n");
  filter.push("        /home/runner/go/pkg/mod/.../cpu_null.go:38\n");
  filter.push("2026-08-16T21:48:18.849+0800 INFO starting LiveKit server\n");
  filter.push(`2026-08-16T21:48:18.900+0800 ERROR ${WINDOWS_CPU_WARNING_TEXT}\n`);
  filter.flush();

  assert.deepEqual(output, [
    "2026-08-16T21:48:18.807+0800 INFO starting\n",
    "2026-08-16T21:48:18.849+0800 INFO starting LiveKit server\n",
  ]);
  assert.equal(getWarningCount(), 1);
});

test("LiveKit 非 CPU 错误和过滤后的下一条日志保持原样", () => {
  const { filter, output } = collectFilterOutput();

  filter.push(`2026-08-16T21:48:18.807+0800 ERROR ${WINDOWS_CPU_WARNING_TEXT}\n`);
  filter.push("2026-08-16T21:48:18.849+0800 ERROR port already in use\n");
  filter.push("2026-08-16T21:48:18.900+0800 WARN network is slow\n");
  filter.flush();

  assert.deepEqual(output, [
    "2026-08-16T21:48:18.849+0800 ERROR port already in use\n",
    "2026-08-16T21:48:18.900+0800 WARN network is slow\n",
  ]);
});

test("没有时间戳的真实错误不会被误判为 Go 堆栈", () => {
  const { filter, output } = collectFilterOutput();

  filter.push(`2026-08-16T21:48:18.807+0800 ERROR ${WINDOWS_CPU_WARNING_TEXT}\n`);
  filter.push("fatal: unable to bind RTC port\n");
  filter.flush();

  assert.deepEqual(output, ["fatal: unable to bind RTC port\n"]);
});

test("UTC Z 时间戳可以结束 Go 堆栈过滤", () => {
  const { filter, output } = collectFilterOutput();

  filter.push(`2026-08-16T21:48:18.807+0800 ERROR ${WINDOWS_CPU_WARNING_TEXT}\n`);
  filter.push("github.com/livekit/livekit-server/pkg/service.startServer\n");
  filter.push("        /home/runner/go/pkg/mod/livekit/server.go:295\n");
  filter.push("2026-08-16T21:48:18.849Z INFO server is ready\n");
  filter.flush();

  assert.deepEqual(output, ["2026-08-16T21:48:18.849Z INFO server is ready\n"]);
});

test("过滤器保留 CRLF 和没有换行符的原始结尾", () => {
  const { filter, output } = collectFilterOutput();

  filter.push("normal log\r\nlast log");
  filter.flush();

  assert.deepEqual(output, ["normal log\r\n", "last log"]);
});

test("非 Windows 平台不修改 LiveKit 原始 CPU 提示", () => {
  const output = [];
  const filter = createLiveKitLogFilter({
    platform: "linux",
    write: text => output.push(text),
    onKnownWindowsCpuWarning: () => {
      throw new Error("不应在非 Windows 平台触发 Windows 过滤器");
    },
  });

  const line = `2026-08-16T21:48:18.807+0800 ERROR ${WINDOWS_CPU_WARNING_TEXT}\n`;
  filter.push(line);
  filter.flush();

  assert.deepEqual(output, [line]);
});
