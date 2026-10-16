import { execFile } from "node:child_process";
import { promisify } from "node:util";

import type {
  SelectionCopyResult,
  WindowsSelectionCapturePort
} from "./selectionClipboardTransaction";

const execFileAsync = promisify(execFile);

type PowerShellRunner = (script: string) => Promise<string>;

type CreateWindowsSelectionCapturePortOptions = {
  runPowerShell?: PowerShellRunner;
};

/**
 * 创建一次性完成复制、就绪等待和文件路径读取的 Windows 捕获端口。
 *
 * @param options 可注入的 PowerShell 执行器。
 * @returns 不读取文件正文的系统选区捕获端口。
 */
export function createWindowsSelectionCapturePort(
  options: CreateWindowsSelectionCapturePortOptions = {}
): WindowsSelectionCapturePort {
  const runPowerShell = options.runPowerShell ?? runWindowsSelectionPowerShell;
  return {
    async captureSelection(): Promise<SelectionCopyResult> {
      return parseSelectionCopyResult(await runPowerShell(WINDOWS_SELECTION_CAPTURE_COMMAND));
    }
  };
}

async function runWindowsSelectionPowerShell(script: string): Promise<string> {
  const { stdout } = await execFileAsync("powershell.exe", [
    "-NoProfile",
    "-NonInteractive",
    "-STA",
    "-Command",
    script
  ], {
    windowsHide: true
  });
  return stdout;
}

function parseSelectionCopyResult(output: string): SelectionCopyResult {
  try {
    const encodedPayload = output.trim();
    const payload: unknown = JSON.parse(Buffer.from(encodedPayload, "base64").toString("utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("selection capture response is invalid");
    }
    const candidate = payload as Record<string, unknown>;
    if (typeof candidate.clipboardChanged !== "boolean"
      || typeof candidate.elapsedMs !== "number"
      || !Number.isInteger(candidate.elapsedMs)
      || candidate.elapsedMs < 0
      || !Array.isArray(candidate.filePaths)) {
      throw new Error("selection capture response fields are invalid");
    }
    const filePaths = candidate.filePaths.flatMap(path => (
      typeof path === "string" && path.trim() ? [path.trim()] : []
    ));
    return {
      filePaths,
      clipboardChanged: candidate.clipboardChanged,
      elapsedMs: candidate.elapsedMs
    };
  } catch (error) {
    throw new Error("Windows selection capture response is invalid", { cause: error });
  }
}

const WINDOWS_SELECTION_CAPTURE_COMMAND = `
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class SelectionClipboardState {
  [DllImport("user32.dll")]
  public static extern short GetAsyncKeyState(int virtualKeyCode);

  [DllImport("user32.dll")]
  public static extern uint GetClipboardSequenceNumber();
}
"@

function Test-KeyDown([int] $VirtualKeyCode) {
  return (([SelectionClipboardState]::GetAsyncKeyState($VirtualKeyCode) -band 0x8000) -ne 0)
}

function Wait-ModifierKeysReleased([int] $TimeoutMs = 1500) {
  $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
  $modifierKeys = @(0x10, 0x11, 0x12, 0x5B, 0x5C)
  while ([DateTime]::UtcNow -lt $deadline) {
    $isAnyModifierDown = $false
    foreach ($key in $modifierKeys) {
      if (Test-KeyDown $key) {
        $isAnyModifierDown = $true
        break
      }
    }
    if (-not $isAnyModifierDown) {
      return
    }
    Start-Sleep -Milliseconds 20
  }
}

function Write-SelectionResult(
  [string[]] $FilePaths,
  [bool] $ClipboardChanged,
  [int] $ElapsedMs
) {
  $payload = @{
    filePaths = @($FilePaths)
    clipboardChanged = $ClipboardChanged
    elapsedMs = $ElapsedMs
  }
  $json = ConvertTo-Json -Compress -InputObject $payload
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
  [System.Convert]::ToBase64String($bytes)
}

Wait-ModifierKeysReleased
$baselineSequence = [SelectionClipboardState]::GetClipboardSequenceNumber()
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
[System.Windows.Forms.SendKeys]::SendWait('^c')
$clipboardChanged = $false

while ($stopwatch.ElapsedMilliseconds -lt 1500) {
  if ([SelectionClipboardState]::GetClipboardSequenceNumber() -ne $baselineSequence) {
    $clipboardChanged = $true
    break
  }
  Start-Sleep -Milliseconds 20
}

$paths = @()
if ($clipboardChanged) {
  # Shell 可能采用延迟渲染；短暂重试 FileDrop，同时让文本和图片选区快速返回 Electron。
  for ($attempt = 0; $attempt -lt 10; $attempt += 1) {
    try {
      if ([System.Windows.Forms.Clipboard]::ContainsFileDropList()) {
        foreach ($path in [System.Windows.Forms.Clipboard]::GetFileDropList()) {
          $paths += [string] $path
        }
        break
      }
      $containsText = [System.Windows.Forms.Clipboard]::ContainsText()
      $containsImage = [System.Windows.Forms.Clipboard]::ContainsImage()
      $containsHtml = [System.Windows.Forms.Clipboard]::ContainsData([System.Windows.Forms.DataFormats]::Html)
      if ($containsText -or $containsImage -or $containsHtml) {
        break
      }
    } catch [System.Runtime.InteropServices.ExternalException] {
      # 剪贴板被 Shell 短暂占用时继续等待，不把瞬时竞争误报为空选区。
    }
    Start-Sleep -Milliseconds 20
  }
}

$stopwatch.Stop()
Write-SelectionResult -FilePaths $paths -ClipboardChanged $clipboardChanged -ElapsedMs ([int] $stopwatch.ElapsedMilliseconds)
`;
