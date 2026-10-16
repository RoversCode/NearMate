import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createBootstrapConsoleLauncher } from "./bootstrapConsoleLauncher";

const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => {
    server.closeAllConnections();
    server.close(error => error ? reject(error) : resolve());
  })));
});

/**
 * 用真实本机 HTTP 响应核对初始化接口，浏览器打开仍由测试替身记录。
 * Args:
 *   payload: 控制面应返回的状态。
 *   statusCode: 响应码。
 * Returns:
 *   服务地址和收到的请求记录。
 */
async function serveBootstrapStatus(payload: unknown, statusCode = 200) {
  const requests: string[] = [];
  const server = createServer((request, response) => {
    requests.push(request.url ?? "");
    response.writeHead(statusCode, { "Content-Type": "application/json" });
    response.end(JSON.stringify(payload));
  });
  servers.push(server);
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  return { controlUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests };
}

describe("首次初始化控制台入口", () => {
  it("真实未初始化响应只触发一次本地控制台，重复与并发调用均不重开", async () => {
    const endpoint = await serveBootstrapStatus({ is_initialized: false, status: "required" });
    const openExternal = vi.fn().mockResolvedValue(undefined);
    const launcher = createBootstrapConsoleLauncher({ ...endpoint, openExternal, isAppQuitting: () => false, onFailure: vi.fn() });
    await Promise.all([launcher(), launcher()]);
    await launcher();
    expect(endpoint.requests).toEqual(["/api/bootstrap/status"]);
    expect(openExternal).toHaveBeenCalledOnce();
    expect(openExternal).toHaveBeenCalledWith(`${endpoint.controlUrl}/console`);
  });

  it("生产入口指定设置页时只在控制台地址上追加 section 查询参数", async () => {
    const endpoint = await serveBootstrapStatus({ is_initialized: false });
    const openExternal = vi.fn().mockResolvedValue(undefined);

    await createBootstrapConsoleLauncher({
      ...endpoint,
      consoleSection: "settings",
      openExternal,
      isAppQuitting: () => false,
      onFailure: vi.fn()
    })();

    expect(openExternal).toHaveBeenCalledWith(`${endpoint.controlUrl}/console?section=settings`);
  });

  it("已初始化的真实响应不会打开浏览器", async () => {
    const endpoint = await serveBootstrapStatus({ is_initialized: true });
    const openExternal = vi.fn();
    await createBootstrapConsoleLauncher({ ...endpoint, openExternal, isAppQuitting: () => false, onFailure: vi.fn() })();
    expect(openExternal).not.toHaveBeenCalled();
  });

  it.each([{ payload: { is_initialized: "false" }, code: 200 }, { payload: { is_initialized: false }, code: 503 }])("错误或不合规响应保留手动入口且不自动重试：$code", async ({ payload, code }) => {
    const endpoint = await serveBootstrapStatus(payload, code);
    const openExternal = vi.fn();
    const onFailure = vi.fn();
    const launcher = createBootstrapConsoleLauncher({ ...endpoint, openExternal, isAppQuitting: () => false, onFailure });
    await launcher(); await launcher();
    expect(endpoint.requests).toHaveLength(1);
    expect(openExternal).not.toHaveBeenCalled();
    expect(onFailure).toHaveBeenCalledOnce();
  });

  it("请求结束前开始退出时不再打开控制台", async () => {
    const endpoint = await serveBootstrapStatus({ is_initialized: false });
    const openExternal = vi.fn();
    const isAppQuitting = vi.fn().mockReturnValueOnce(false).mockReturnValue(true);
    await createBootstrapConsoleLauncher({ ...endpoint, openExternal, isAppQuitting, onFailure: vi.fn() })();
    expect(openExternal).not.toHaveBeenCalled();
  });
});
