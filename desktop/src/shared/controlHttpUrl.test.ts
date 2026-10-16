import { describe, expect, it } from "vitest";
import {
  resolveControlConsoleUrl,
  resolveControlHttpRootUrl,
  resolveControlWebSocketUrl
} from "./controlHttpUrl";

describe("control HTTP URL normalization", () => {
  it.each([
    {
      name: "环境变量缺失时使用本地默认根地址",
      input: undefined,
      expectedRootUrl: "http://127.0.0.1:8765",
      expectedConsoleUrl: "http://127.0.0.1:8765/console"
    },
    {
      name: "服务根地址带末尾斜杠时移除末尾斜杠",
      input: "http://127.0.0.1:8765/",
      expectedRootUrl: "http://127.0.0.1:8765",
      expectedConsoleUrl: "http://127.0.0.1:8765/console"
    },
    {
      name: "误配为保留 console 页面时回退到控制服务根地址",
      input: "http://127.0.0.1:8765/console",
      expectedRootUrl: "http://127.0.0.1:8765",
      expectedConsoleUrl: "http://127.0.0.1:8765/console"
    },
    {
      name: "误配为带斜杠的保留 console 页面时回退到控制服务根地址",
      input: "http://127.0.0.1:8765/console/",
      expectedRootUrl: "http://127.0.0.1:8765",
      expectedConsoleUrl: "http://127.0.0.1:8765/console"
    }
  ])("会把 COMPANION_CONTROL_HTTP_URL 统一解释为控制 HTTP 根地址：$name", ({
    input,
    expectedRootUrl,
    expectedConsoleUrl
  }) => {
    expect(resolveControlHttpRootUrl(input)).toBe(expectedRootUrl);
    expect(resolveControlConsoleUrl(input)).toBe(expectedConsoleUrl);
  });

  it("WebSocket 必须从同一个本机控制 Origin 派生", () => {
    expect(resolveControlWebSocketUrl("http://127.0.0.1:9000/console")).toBe(
      "ws://127.0.0.1:9000/ws/control"
    );
  });

  it("允许显式 IPv6 loopback，并从同一 Origin 派生 WebSocket", () => {
    expect(resolveControlHttpRootUrl("http://[::1]:8765")).toBe("http://[::1]:8765");
    expect(resolveControlWebSocketUrl("http://[::1]:8765")).toBe(
      "ws://[::1]:8765/ws/control"
    );
  });

  it.each([
    "https://127.0.0.1:8765",
    "http://localhost:8765",
    "http://example.test:8765",
    "http://127.0.0.1:8765/base",
    "http://user:secret@127.0.0.1:8765"
  ])("拒绝非本机控制服务地址：%s", input => {
    expect(() => resolveControlHttpRootUrl(input)).toThrow();
  });
});
