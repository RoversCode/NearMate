import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchLogs } from "./records";

afterEach(() => vi.unstubAllGlobals());

describe("fetchLogs", () => {
  it("读取带序号和结构化 payload 的本次运行日志", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{
      sequence: 7,
      event_type: "activities.snapshot_changed",
      payload: { revision: 3, active: [] },
      recorded_at: "2026-07-18T10:00:00+08:00"
    }]), { headers: { "Content-Type": "application/json" } })));

    await expect(fetchLogs("http://localhost")).resolves.toEqual([{
      sequence: 7,
      event_type: "activities.snapshot_changed",
      payload: { revision: 3, active: [] },
      recorded_at: "2026-07-18T10:00:00+08:00"
    }]);
  });

  it("不再接受旧 payload_json 字符串协议", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify([{
      event_type: "legacy",
      payload_json: "{}",
      recorded_at: "2026-07-18T10:00:00+08:00"
    }]), { headers: { "Content-Type": "application/json" } })));

    await expect(fetchLogs("http://localhost")).rejects.toThrow("运行日志响应格式无效");
  });
});
