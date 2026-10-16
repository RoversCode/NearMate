import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import test from "node:test";
import { buildSpawnSpec } from "./dev-process.mjs";

test("Windows 下 pnpm.cmd 使用 cmd.exe 启动且不打开 Node shell", async t => {
  if (process.platform !== "win32") {
    t.skip("该回归覆盖 Windows 的 cmd shim 行为");
    return;
  }

  const spec = buildSpawnSpec("pnpm.cmd", ["--version"]);
  assert.equal(spec.shell, false);
  assert.match(spec.executable.toLowerCase(), /(^|\\)cmd\.exe$/);

  const result = await new Promise((resolve, reject) => {
    const child = spawn(spec.executable, spec.args, {
      shell: spec.shell,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", chunk => { stdout += chunk.toString(); });
    child.stderr.on("data", chunk => { stderr += chunk.toString(); });
    child.once("error", reject);
    child.once("exit", (code, signal) => resolve({ code, signal, stdout, stderr }));
  });

  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /\d+\.\d+\.\d+/);
});
