// @vitest-environment node

import { describe, expect, it } from "vitest";
import config from "./vite.config";

describe("Console 开发服务器端口契约", () => {
  it("固定共享端口并拒绝自动顺延", () => {
    expect(config.server).toMatchObject({
      host: "127.0.0.1",
      port: 5173,
      strictPort: true
    });
    expect(config.server?.proxy).toMatchObject({
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765" }
    });
  });
});
