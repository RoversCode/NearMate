import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { main, preflightDevelopmentPorts } from "./dev.mjs";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const EXPECTED_SERVICES = ["livekit:dev", "backend:core", "desktop:dev", "console:dev"];

test("开发编排只启动仍然存在的四个服务", () => {
  const result = spawnSync(process.execPath, ["scripts/dev.mjs", "--dry-run"], {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8"
  });

  assert.equal(result.status, 0, result.stderr);
  const serviceCommands = result.stdout
    .trim()
    .split(/\r?\n/)
    .map(parseDryRunLine);
  assert.deepEqual(
    serviceCommands.map(({ serviceName }) => serviceName),
    EXPECTED_SERVICES
  );

  const packageJson = JSON.parse(readFileSync(path.join(REPOSITORY_ROOT, "package.json"), "utf8"));
  for (const { serviceName, scriptName } of serviceCommands) {
    assert.equal(scriptName, serviceName);
    assert.equal(typeof packageJson.scripts[scriptName], "string", `缺少根脚本：${scriptName}`);
  }
  assert.equal(packageJson.scripts["backend:worker"], undefined);
});

test("根编排预检冲突时不创建任何子进程", async () => {
  const ports = {
    backend: { host: "127.0.0.1", port: 8765 },
    console: { host: "127.0.0.1", port: 5173 },
    desktop: { host: "127.0.0.1", port: 5174 },
    livekit: { host: "127.0.0.1", port: 7880 }
  };
  const startedServices = [];
  const originalError = console.error;
  console.error = () => {};
  try {
    const ok = await preflightDevelopmentPorts({
      ports,
      dependencies: {
        probe: async endpoint => {
          if (endpoint.port === 5173) {
            const error = new Error("占用");
            error.code = "EADDRINUSE";
            throw error;
          }
        },
        lookupOwner: async () => ({ pid: 7, processName: "node.exe" })
      }
    });
    assert.equal(ok, false);
    await main([], {
      ports,
      dependencies: {
        probe: async endpoint => {
          if (endpoint.port === 5173) {
            const error = new Error("占用");
            error.code = "EADDRINUSE";
            throw error;
          }
        },
        lookupOwner: async () => ({ pid: 7, processName: "node.exe" })
      },
      startServiceImpl: service => {
        startedServices.push(service.name);
        throw new Error("不应启动");
      }
    });
    assert.deepEqual(startedServices, []);
    process.exitCode = undefined;
  } finally {
    console.error = originalError;
    process.exitCode = undefined;
  }
});

/**
 * 解析开发编排 dry-run 的单行输出。
 *
 * @param {string} line 带服务名和 pnpm 命令的输出行。
 * @returns {{ serviceName: string, scriptName: string }} 服务名与实际调用的根脚本名。
 */
function parseDryRunLine(line) {
  const matched = /^\[([^\]]+)]\s+pnpm(?:\.cmd)?\s+(\S+)$/.exec(line.trim());
  assert.notEqual(matched, null, `无法解析开发编排输出：${line}`);
  return { serviceName: matched[1], scriptName: matched[2] };
}
