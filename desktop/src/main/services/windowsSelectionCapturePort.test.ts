import { execFileSync } from "node:child_process";
import { describe, expect, it, vi } from "vitest";
import { createWindowsSelectionCapturePort } from "./windowsSelectionCapturePort";

describe("createWindowsSelectionCapturePort", () => {
  it("在一个辅助进程中等待剪贴板变化并读取完整 FileDrop 路径", async () => {
    const filePaths = Array.from({ length: 40 }, (_, index) => `G:\\docs\\item-${index}.txt`);
    const encoded = Buffer.from(JSON.stringify({
      filePaths,
      clipboardChanged: true,
      elapsedMs: 386
    }), "utf8").toString("base64");
    const runPowerShell = vi.fn(async (_script: string) => encoded);
    const port = createWindowsSelectionCapturePort({ runPowerShell });

    await expect(port.captureSelection()).resolves.toEqual({
      filePaths,
      clipboardChanged: true,
      elapsedMs: 386
    });

    const script = runPowerShell.mock.calls[0]?.[0];
    expect(script).toBeTypeOf("string");
    if (!script) {
      throw new Error("未捕获 PowerShell 脚本");
    }
    expect(script).toContain("Wait-ModifierKeysReleased");
    expect(script).toContain("GetClipboardSequenceNumber");
    expect(script).toContain("ElapsedMilliseconds -lt 1500");
    expect(script).toContain("Start-Sleep -Milliseconds 20");
    expect(script).toContain("GetFileDropList");
    expect(script).toContain("DataFormats]::Html");
    expect(script).not.toContain("Start-Sleep -Milliseconds 120");
    expect(script.indexOf("Wait-ModifierKeysReleased")).toBeLessThan(script.indexOf("SendWait('^c')"));
    expect(() => parseWithWindowsPowerShell(script)).not.toThrow();
  });

  it("非文件选区返回空路径但保留剪贴板已变化事实", async () => {
    const encoded = Buffer.from(JSON.stringify({
      filePaths: [],
      clipboardChanged: true,
      elapsedMs: 42
    }), "utf8").toString("base64");
    const port = createWindowsSelectionCapturePort({ runPowerShell: vi.fn(async () => encoded) });

    await expect(port.captureSelection()).resolves.toEqual({
      filePaths: [],
      clipboardChanged: true,
      elapsedMs: 42
    });
  });
});

/**
 * 使用系统自带 PowerShell 解析器验证生产脚本语法。
 *
 * @param script 需要验证的捕获脚本。
 */
function parseWithWindowsPowerShell(script: string): void {
  if (process.platform !== "win32") {
    return;
  }

  const sourceBase64 = Buffer.from(script, "utf8").toString("base64");
  const parserCommand = `
$source = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('${sourceBase64}'))
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseInput($source, [ref] $tokens, [ref] $errors) | Out-Null
if ($errors.Count -gt 0) {
  $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }
  exit 1
}
`;
  execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", parserCommand], {
    windowsHide: true,
    stdio: "pipe"
  });
}
