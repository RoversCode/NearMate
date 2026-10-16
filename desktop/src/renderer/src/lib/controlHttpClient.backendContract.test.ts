import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { createControlHttpClient } from "./controlHttpClient";
import type { SettingsSnapshot } from "../types/settingsSnapshot";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../../../..");
const pythonPath = process.env.NEARMATE_TEST_PYTHON ?? join(repositoryRoot, ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python");

// Desktop 单独安装依赖时可不含 Python；完整仓库验证使用已安装的 Backend 环境运行此合同。
describe.skipIf(!existsSync(pythonPath))("Desktop 与真实 Backend 设置 HTTP 合同", () => {
  let server: ChildProcessWithoutNullStreams;
  let baseUrl: string;

  beforeAll(async () => {
    // 只启动隔离的真实设置路由和仓储，不连接用户安装目录或媒体服务。
    server = spawn(pythonPath, ["-B", "-X", "utf8",
      join(repositoryRoot, "backend/tests/support/settings_http_contract_server.py")],
    { cwd: repositoryRoot, windowsHide: true, stdio: "pipe" });
    baseUrl = await new Promise<string>((resolveReady, reject) => {
      let output = "";
      let errors = "";
      server.stderr.on("data", chunk => { errors += String(chunk); });
      server.stdout.on("data", chunk => {
        output += String(chunk);
        for (const line of output.split(/\r?\n/)) {
          try {
            const ready = JSON.parse(line) as { base_url?: string };
            if (ready.base_url) resolveReady(ready.base_url);
          } catch { /* 启动日志不是 ready JSON 时继续等待。 */ }
        }
      });
      server.once("error", reject);
      server.once("exit", code => reject(new Error(`设置合同服务器退出 ${code}: ${errors}`)));
    });
  }, 20000);

  afterAll(async () => {
    if (!server || server.exitCode !== null) return;
    // EOF 由 fixture 负责优雅退出并清理它自己的临时目录。
    await new Promise<void>(resolveClosed => {
      const timeout = setTimeout(() => { server.kill(); resolveClosed(); }, 5000);
      server.once("exit", () => { clearTimeout(timeout); resolveClosed(); });
      server.stdin.end();
    });
  });

  beforeEach(async () => {
    const response = await fetch(`${baseUrl}/test/reset`, { method: "POST" });
    expect(response.status).toBe(200);
  });

  it("旧Desktop完整投影被真实schema拒绝：缺少版本且携带只读字幕字段", async () => {
    const client = createControlHttpClient(baseUrl);
    const oldProjection = await client.fetchSettings({ requireAuthoritative: true });
    const response = await fetch(`${baseUrl}/api/settings`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(oldProjection)
    });
    expect(response.status).toBe(422);
    const result = await response.json() as { detail: Array<{ type: string; loc: string[] }> };
    expect(result.detail).toEqual(expect.arrayContaining([
      expect.objectContaining({ type: "missing", loc: ["body", "expected_revision"] }),
      expect.objectContaining({ type: "extra_forbidden", loc: ["body", "active_character_caption_timing"] })
    ]));
  });

  it("实际client切换人物获得202并持久化，用户简介和三类快捷键保持原值", async () => {
    const before = await (await fetch(`${baseUrl}/api/settings`)).json() as SettingsSnapshot;
    const committed: SettingsSnapshot[] = [];
    const client = createControlHttpClient(baseUrl);
    await client.updateSettings({ selected_live2d_model_id: "Natori" }, snapshot => committed.push(snapshot));
    await client.updateSettings({ selected_live2d_model_id: "Rice" }, snapshot => committed.push(snapshot));
    const evidence = await (await fetch(`${baseUrl}/test/evidence`)).json() as {
      snapshot: SettingsSnapshot; persisted: Record<string, unknown>;
      posts: Array<{ status_code: number; body: Record<string, unknown> }>;
    };
    expect(evidence.posts.map(post => post.status_code)).toEqual([202, 202]);
    expect(evidence.snapshot.selected_live2d_model_id).toBe("Rice");
    expect(evidence.snapshot.settings_revision).toBe(before.settings_revision + 2);
    expect(evidence.persisted.selected_live2d_model_id).toBe("Rice");
    expect(evidence.persisted.settings_revision).toBe(before.settings_revision + 2);
    for (const key of Object.keys(before) as Array<keyof SettingsSnapshot>) {
      if (key === "settings_revision" || key === "selected_live2d_model_id") continue;
      expect(evidence.snapshot[key]).toEqual(before[key]);
    }
    expect(committed.at(-1)).toEqual(evidence.snapshot);
    expect(evidence.posts[0]!.body.expected_revision).toBe(before.settings_revision);
    expect(evidence.posts[0]!.body).not.toHaveProperty("active_character_caption_timing");
  });

  it("真实事务并发修改导致409时，只提交一次且不覆盖新版本或产生成功回调", async () => {
    const before = await (await fetch(`${baseUrl}/api/settings`)).json() as SettingsSnapshot;
    await fetch(`${baseUrl}/test/conflict`, { method: "POST" });
    const onCommitted = vi.fn();
    const client = createControlHttpClient(baseUrl);
    await expect(client.updateSettings({ selected_live2d_model_id: "Natori" }, onCommitted))
      .rejects.toThrow("settings update request failed: 409");
    const evidence = await (await fetch(`${baseUrl}/test/evidence`)).json() as {
      snapshot: SettingsSnapshot; persisted: Record<string, unknown>; posts: Array<{ status_code: number }>;
    };
    expect(evidence.posts).toHaveLength(1);
    expect(evidence.posts[0]!.status_code).toBe(409);
    expect(evidence.snapshot.selected_live2d_model_id).toBe(before.selected_live2d_model_id);
    expect(evidence.persisted.selected_live2d_model_id).toBe(before.selected_live2d_model_id);
    expect(evidence.snapshot.settings_revision).toBe(before.settings_revision + 1);
    expect(evidence.snapshot.user_self_intro).toBe(before.user_self_intro);
    expect(evidence.snapshot.user_extra_notes).toBe(before.user_extra_notes);
    expect(onCommitted).not.toHaveBeenCalled();
  });
});
