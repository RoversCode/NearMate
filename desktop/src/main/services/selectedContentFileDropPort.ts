import { execFile } from "node:child_process";
import { promisify } from "node:util";

import type { SelectedContentFileDropPort } from "./selectionClipboardTransaction";

const execFileAsync = promisify(execFile);

type WindowsFileDropPowerShellRunner = (script: string, scriptArguments?: string[]) => Promise<string>;

type CreateWindowsSelectedContentFileDropPortOptions = {
  runPowerShell?: WindowsFileDropPowerShellRunner;
};

/**
 * createWindowsSelectedContentFileDropPort 创建 Windows 文件列表剪贴板端口。
 *
 * @param options 可选 PowerShell 执行器，测试中用于替换系统调用。
 * @returns 读取和写回 CF_HDROP 文件列表的窄端口。
 */
export function createWindowsSelectedContentFileDropPort(
  options: CreateWindowsSelectedContentFileDropPortOptions = {}
): SelectedContentFileDropPort {
  const runPowerShell = options.runPowerShell ?? runWindowsClipboardPowerShell;
  return {
    async readFilePaths(): Promise<string[]> {
      const output = await runPowerShell(READ_FILE_DROP_SCRIPT);
      return parseFileDropJson(output);
    },

    async writeFilePaths(filePaths): Promise<void> {
      if (filePaths.length === 0) {
        return;
      }

      await runPowerShell(WRITE_FILE_DROP_SCRIPT, [encodeFileDropJson(filePaths)]);
    }
  };
}

async function runWindowsClipboardPowerShell(script: string, scriptArguments: string[] = []): Promise<string> {
  const { stdout } = await execFileAsync("powershell.exe", [
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    script,
    ...scriptArguments
  ], {
    windowsHide: true
  });
  return stdout;
}

function parseFileDropJson(output: string): string[] {
  try {
    const encodedPayload = output.trim();
    if (!encodedPayload) {
      return [];
    }
    const json = Buffer.from(encodedPayload, "base64").toString("utf8");
    const payload: unknown = JSON.parse(json);
    if (typeof payload === "string" && payload.trim().length > 0) {
      return [payload];
    }
    return Array.isArray(payload) ? payload.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
  } catch {
    return [];
  }
}

/**
 * 把文件路径 JSON 编码成只含 ASCII 的 PowerShell 参数。
 *
 * @param filePaths 需要写回 Windows FileDrop 的 Unicode 路径。
 * @returns UTF-8 JSON 的 Base64 文本。
 */
function encodeFileDropJson(filePaths: string[]): string {
  return Buffer.from(JSON.stringify(filePaths), "utf8").toString("base64");
}

const READ_FILE_DROP_SCRIPT = `
& {
Add-Type -AssemblyName System.Windows.Forms
$paths = [System.Windows.Forms.Clipboard]::GetFileDropList()
$items = @()
foreach ($path in $paths) {
  $items += [string] $path
}
$json = ConvertTo-Json -Compress -InputObject @($items)
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
[System.Convert]::ToBase64String($bytes)
}
`;

const WRITE_FILE_DROP_SCRIPT = `
& {
param([string] $FilePathsBase64)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Collections.Specialized
$jsonBytes = [System.Convert]::FromBase64String($FilePathsBase64)
$FilePathsJson = [System.Text.Encoding]::UTF8.GetString($jsonBytes)
$paths = ConvertFrom-Json -InputObject $FilePathsJson
$collection = New-Object System.Collections.Specialized.StringCollection
foreach ($path in $paths) {
  [void] $collection.Add([string] $path)
}
[System.Windows.Forms.Clipboard]::SetFileDropList($collection)
}
`;
