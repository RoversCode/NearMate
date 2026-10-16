import { describe, expect, it, vi } from "vitest";

import { createWindowsSelectedContentFileDropPort } from "./selectedContentFileDropPort";

type TestPowerShellRunner = (script: string, scriptArguments?: string[]) => Promise<string>;

describe("createWindowsSelectedContentFileDropPort", () => {
  it("读取 Windows FileDrop 文件列表", async () => {
    const runPowerShell: ReturnType<typeof vi.fn<TestPowerShellRunner>> = vi.fn(async () =>
      encodePowerShellPayload(["G:\\docs\\电脑硬件信息.txt", "G:\\workspace\\项目"])
    );
    const port = createWindowsSelectedContentFileDropPort({ runPowerShell });

    await expect(port.readFilePaths()).resolves.toEqual(["G:\\docs\\电脑硬件信息.txt", "G:\\workspace\\项目"]);

    expect(runPowerShell.mock.calls[0]?.[0]).toContain("GetFileDropList");
    expect(runPowerShell.mock.calls[0]?.[0]).toContain("ToBase64String");
  });

  it("写回 Windows FileDrop 文件列表", async () => {
    const runPowerShell: ReturnType<typeof vi.fn<TestPowerShellRunner>> = vi.fn(async () => "");
    const port = createWindowsSelectedContentFileDropPort({ runPowerShell });

    await port.writeFilePaths(["G:\\docs\\电脑硬件信息.txt"]);

    expect(runPowerShell.mock.calls[0]?.[0]).toContain("SetFileDropList");
    expect(runPowerShell.mock.calls[0]?.[0]).toContain("FromBase64String");
    expect(runPowerShell.mock.calls[0]?.[1]).toEqual([
      encodePowerShellPayload(["G:\\docs\\电脑硬件信息.txt"])
    ]);
  });

  it("单文件 FileDrop JSON 字符串会被解析成路径数组", async () => {
    const port = createWindowsSelectedContentFileDropPort({
      runPowerShell: vi.fn(async () => encodePowerShellPayload("G:\\docs\\电脑硬件信息.txt"))
    });

    await expect(port.readFilePaths()).resolves.toEqual(["G:\\docs\\电脑硬件信息.txt"]);
  });

  it("FileDrop 响应异常时返回空列表", async () => {
    const port = createWindowsSelectedContentFileDropPort({
      runPowerShell: vi.fn(async () => "not json")
    });

    await expect(port.readFilePaths()).resolves.toEqual([]);
  });
});

function encodePowerShellPayload(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64");
}
