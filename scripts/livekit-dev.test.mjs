import assert from "node:assert/strict";
import test from "node:test";
import { main } from "./livekit-dev.mjs";

test("LiveKit 端口冲突时不会准备或下载可执行文件", async () => {
  let executablePreparationCalled = false;
  const code = await main({
    checkPortImpl: async () => ({
      ok: false,
      serviceName: "livekit",
      endpoint: { host: "127.0.0.1", port: 7880 },
      owner: { pid: 12, processName: "node.exe" }
    }),
    ensureExecutable: async () => {
      executablePreparationCalled = true;
      return "never";
    }
  });

  assert.equal(code, 1);
  assert.equal(executablePreparationCalled, false);
});
