import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { createPackagedRuntimeEnvironment } from "./backendEnvironment";
import { resolveNearMateRuntimeLayout } from "./runtimeLayout";

describe("安装态私有工具环境", () => {
  it.each([
    { base: { SystemRoot: "D:\\Windows", SystemDrive: "D:", ANTHROPIC_API_KEY: "fixture" }, expectedSystemDrive: "D:" },
    { base: { sYsTeMrOoT: "E:\\Windows", sYsTeMdRiVe: "E:" }, expectedSystemDrive: "E:" },
    { base: { SystemRoot: "F:\\Windows" }, expectedSystemDrive: "F:" }
  ])("保留或补齐系统盘 $expectedSystemDrive，避免 Windows 缓存写进安装目录", ({ base, expectedSystemDrive }) => {
    const layout = resolveNearMateRuntimeLayout({ isPackaged: false, repoRoot: "G:\\fixture" });
    layout.manifestPath = join(layout.runtimeRoot, "runtime-manifest.json");
    const environment = createPackagedRuntimeEnvironment(layout, "G:\\user-data", base);

    expect(environment.SystemDrive).toBe(expectedSystemDrive);
    expect(environment.ANTHROPIC_API_KEY).toBeUndefined();
  });

  it("媒体 Skill 与后台子进程共用随包 FFmpeg，宿主 PATH 不影响选择", () => {
    const layout = resolveNearMateRuntimeLayout({ isPackaged: false, repoRoot: "G:\\fixture" });
    layout.manifestPath = join(layout.runtimeRoot, "runtime-manifest.json");
    const base = { SystemRoot: "C:\\Windows", PATH: "C:\\untrusted-tools" };
    const environment = createPackagedRuntimeEnvironment(layout, "G:\\user-data", base);

    expect(environment.PATH?.split(";")).toContain(join(layout.runtimeRoot, "tools", "ffmpeg", "bin"));
    expect(environment.PATH).not.toContain("C:\\untrusted-tools");
    expect(base.PATH).toBe("C:\\untrusted-tools");
  });
});
