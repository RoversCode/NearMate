[CmdletBinding()]
param(
    [string]$Iscc = "C:\Users\$env:USERNAME\AppData\Local\Programs\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Write-Utf8NoBomFile([string]$Path, [string]$Content) {
    <#
    .SYNOPSIS
    用和 compile_setup.ps1 一样的 UTF-8 无 BOM 格式写入 fixture 文件。

    .PARAMETER Path
    要写入的文件路径。

    .PARAMETER Content
    要写入的文本内容。
    #>
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function New-RuntimeManifest([string]$Path, [int]$SchemaVersion = 3) {
    <#
    .SYNOPSIS
    写入安装清理识别所需的最小 runtime-manifest.json。

    .PARAMETER Path
    runtime-manifest.json 的目标路径。

    .PARAMETER SchemaVersion
    要写入的清单版本；v2 用来验证旧安装会被拒绝接管。
    #>
    $manifest = @"
{
  "schema_version": $SchemaVersion,
  "product": "NearMate",
  "platform": "windows-x64",
  "required_files": []
}
"@
    Write-Utf8NoBomFile $Path $manifest
}

function New-Payload([string]$Path, [string[]]$OwnedRootEntries, [int]$SchemaVersion = 3) {
    <#
    .SYNOPSIS
    创建一个极小的 Electron unpacked 目录替身，便于运行真实 Inno Setup。

    .PARAMETER Path
    fixture payload 根目录。

    .PARAMETER OwnedRootEntries
    根级发行文件所有权清单，不包含清单自身时由本函数补上。

    .PARAMETER SchemaVersion
    runtime-manifest.json 的 schema 版本。
    #>
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    foreach ($entry in $OwnedRootEntries) {
        if ($entry -eq 'resources') {
            $runtimeDirectory = Join-Path $Path 'resources\runtime'
            New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
            New-RuntimeManifest (Join-Path $runtimeDirectory 'runtime-manifest.json') $SchemaVersion
            continue
        }
        'fixture release file' | Set-Content -LiteralPath (Join-Path $Path $entry) -Encoding utf8NoBOM
    }
    $manifestEntries = @($OwnedRootEntries + 'release-root-files.txt' | Sort-Object -Unique)
    Write-Utf8NoBomFile (Join-Path $Path 'release-root-files.txt') (($manifestEntries -join "`r`n") + "`r`n")
}

function New-SmokeSetup([string]$FixtureRoot, [string]$PayloadPath, [string]$OutputPath, [string]$AppId, [string]$Name) {
    <#
    .SYNOPSIS
    生成只包含少量 fixture 文件的 Inno Setup 脚本。

    .PARAMETER FixtureRoot
    当前 fixture 的临时根目录。

    .PARAMETER PayloadPath
    要打包的 fixture payload 根目录。

    .PARAMETER OutputPath
    setup.exe 的输出目录。

    .PARAMETER AppId
    本次 fixture 使用的安装器 AppId。

    .PARAMETER Name
    输出安装程序的文件名基名。
    #>
    $cleanupPath = Join-Path $FixtureRoot 'releaseCleanup.iss'
    $scriptPath = Join-Path $FixtureRoot "$Name.iss"
    $payload = [IO.Path]::GetFullPath($PayloadPath)
    $output = [IO.Path]::GetFullPath($OutputPath)
    $cleanup = [IO.Path]::GetFullPath($cleanupPath)
    $cleanupInclude = $cleanup.Replace('"', '""')
    $script = @"
#define PayloadRoot "$payload"
[Setup]
AppId={{$AppId}
AppName=NearMate release cleanup smoke
AppVersion=0.1.0
DefaultDirName={localappdata}\NearMate release cleanup smoke
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=$output
OutputBaseFilename=$Name
Uninstallable=no
DisableProgramGroupPage=yes
Compression=zip

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Code]
#include "$cleanupInclude"

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    CleanPreviousNearMateRelease();
end;
"@
    Set-Content -LiteralPath $scriptPath -Value $script -Encoding ascii
    & $Iscc '/Qp' $scriptPath *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup fixture 编译失败，退出码：$LASTEXITCODE"
    }
    $setupPath = Join-Path $output "$Name.exe"
    if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) {
        throw "Inno Setup fixture 未生成安装程序：$setupPath"
    }
    return $setupPath
}

function Invoke-SmokeInstall([string]$SetupPath, [string]$InstallPath, [string]$LogPath) {
    <#
    .SYNOPSIS
    静默安装 fixture，并在失败时返回 Inno Setup 的退出码。

    .PARAMETER SetupPath
    要执行的 setup.exe。

    .PARAMETER InstallPath
    安装目标目录。

    .PARAMETER LogPath
    Inno Setup 安装日志路径。
    #>
    $arguments = @('/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
        "/DIR=`"$InstallPath`"", "/LOG=`"$LogPath`"")
    $process = Start-Process -FilePath $SetupPath -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    return $process.ExitCode
}

if (-not (Test-Path -LiteralPath $Iscc -PathType Leaf)) {
    throw "找不到 Inno Setup 编译器：$Iscc"
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-release-cleanup-smoke-$([guid]::NewGuid())"
$oldPayload = Join-Path $fixtureRoot 'old-payload'
$newPayload = Join-Path $fixtureRoot 'new-payload'
$legacyPayload = Join-Path $fixtureRoot 'legacy-payload'
$oldOutput = Join-Path $fixtureRoot 'old-output'
$newOutput = Join-Path $fixtureRoot 'new-output'
$legacyOutput = Join-Path $fixtureRoot 'legacy-output'
$corruptOutput = Join-Path $fixtureRoot 'corrupt-output'
$upgradeDirectory = Join-Path $fixtureRoot 'upgrade-install'
$reparseDirectory = Join-Path $fixtureRoot 'reparse-install'
$outsideDirectory = Join-Path $fixtureRoot 'outside-target'
$legacyDirectory = Join-Path $fixtureRoot 'legacy-install'
$missingOwnershipDirectory = Join-Path $fixtureRoot 'missing-ownership-install'
$corruptOwnershipDirectory = Join-Path $fixtureRoot 'corrupt-ownership-install'
New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot 'desktop\installer\releaseCleanup.iss') -Destination $fixtureRoot -Force

try {
    # 第一代发行带有一个下一代不再拥有的文件；该文件应该只在升级清理阶段被删除。
    New-Payload $oldPayload @('NearMate.exe', 'resources', 'obsolete-runtime.dll')
    New-Payload $newPayload @('NearMate.exe', 'resources', 'current-runtime.dll')
    $conflictDirectory = Join-Path $newPayload 'conflict-directory'
    New-Item -ItemType Directory -Path $conflictDirectory -Force | Out-Null
    'new release file' | Set-Content -LiteralPath (Join-Path $conflictDirectory 'new-runtime.dll') -Encoding utf8NoBOM
    New-Payload $legacyPayload @('NearMate.exe', 'resources') 2

    $oldSetup = New-SmokeSetup $fixtureRoot $oldPayload $oldOutput 'B4B5F3CE-4E0B-4E6E-9D32-BD07AE9A3C91' 'old-release'
    $newSetup = New-SmokeSetup $fixtureRoot $newPayload $newOutput 'B4B5F3CE-4E0B-4E6E-9D32-BD07AE9A3C91' 'new-release'
    $legacySetup = New-SmokeSetup $fixtureRoot $legacyPayload $legacyOutput 'C5C6F4DF-5F1C-4F7F-AE43-CE18BFAB4DA2' 'legacy-release'
    $corruptSetup = New-SmokeSetup $fixtureRoot $newPayload $corruptOutput 'D6D7F5E0-6A2D-4080-BF54-DF29C0BC5EB3' 'corrupt-release'

    # 真实用户首次安装经常会先在资源管理器中创建目标目录；空目录同样必须允许接管。
    New-Item -ItemType Directory -Path $upgradeDirectory -Force | Out-Null
    $firstExitCode = Invoke-SmokeInstall $oldSetup $upgradeDirectory (Join-Path $fixtureRoot 'first-install.log')
    if ($firstExitCode -ne 0) { throw "首次 fixture 安装失败，退出码：$firstExitCode" }
    if (-not (Test-Path -LiteralPath (Join-Path $upgradeDirectory 'obsolete-runtime.dll'))) {
        throw '首次 fixture 安装没有写入旧发行文件'
    }

    # 该文件不在第一代所有权清单中，升级绝不能把它当成程序文件删除。
    'user-owned data' | Set-Content -LiteralPath (Join-Path $upgradeDirectory 'user-note.txt') -Encoding utf8NoBOM
    $secondExitCode = Invoke-SmokeInstall $newSetup $upgradeDirectory (Join-Path $fixtureRoot 'upgrade-install.log')
    if ($secondExitCode -ne 0) { throw "fixture 升级安装失败，退出码：$secondExitCode" }
    if (Test-Path -LiteralPath (Join-Path $upgradeDirectory 'obsolete-runtime.dll')) {
        throw '升级没有删除旧所有权清单中的过时程序文件'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $upgradeDirectory 'current-runtime.dll'))) {
        throw '升级没有安装新发行文件'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $upgradeDirectory 'user-note.txt'))) {
        throw '升级误删了所有权清单之外的用户文件'
    }

    # 有效旧发行也不能容忍用户在根目录放置联接，否则新版本复制同名目录可能越出安装根。
    New-Item -ItemType Directory -Path $reparseDirectory -Force | Out-Null
    $reparseFirstExitCode = Invoke-SmokeInstall $oldSetup $reparseDirectory (Join-Path $fixtureRoot 'reparse-first-install.log')
    if ($reparseFirstExitCode -ne 0) { throw "联接 fixture 首次安装失败，退出码：$reparseFirstExitCode" }
    New-Item -ItemType Directory -Path $outsideDirectory -Force | Out-Null
    $reparseTarget = Join-Path $reparseDirectory 'conflict-directory'
    New-Item -ItemType Junction -Path $reparseTarget -Target $outsideDirectory | Out-Null
    $reparseExitCode = Invoke-SmokeInstall $newSetup $reparseDirectory (Join-Path $fixtureRoot 'reparse-upgrade.log')
    if ($reparseExitCode -eq 0) { throw '有效旧发行中的根目录联接被错误跟随升级' }
    if (Test-Path -LiteralPath (Join-Path $outsideDirectory 'new-runtime.dll')) {
        throw '升级拒绝前仍向根目录联接目标写入了发行文件'
    }
    Remove-Item -LiteralPath $reparseTarget -Force

    # schema v2 没有可验证的发行身份；安装器必须拒绝接管非空目录并保留旧文件。
    New-Item -ItemType Directory -Path $legacyDirectory -Force | Out-Null
    New-Payload $legacyDirectory @('NearMate.exe', 'resources') 2
    'legacy user data' | Set-Content -LiteralPath (Join-Path $legacyDirectory 'legacy-note.txt') -Encoding utf8NoBOM
    $legacyExitCode = Invoke-SmokeInstall $legacySetup $legacyDirectory (Join-Path $fixtureRoot 'legacy-install.log')
    if ($legacyExitCode -eq 0) { throw 'schema v2 非空目录被错误接管'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $legacyDirectory 'legacy-note.txt'))) {
        throw 'schema v2 拒绝后用户文件没有保留'
    }

    # 即使 runtime-manifest 已经是 v3，缺少所有权清单也不能猜测旧文件归属。
    $missingRuntimeDirectory = Join-Path $missingOwnershipDirectory 'resources\runtime'
    New-Item -ItemType Directory -Path $missingRuntimeDirectory -Force | Out-Null
    'legacy program' | Set-Content -LiteralPath (Join-Path $missingOwnershipDirectory 'NearMate.exe') -Encoding utf8NoBOM
    New-RuntimeManifest (Join-Path $missingRuntimeDirectory 'runtime-manifest.json')
    'missing ownership user data' | Set-Content -LiteralPath (Join-Path $missingOwnershipDirectory 'missing-note.txt') -Encoding utf8NoBOM
    $missingOwnershipExitCode = Invoke-SmokeInstall $legacySetup $missingOwnershipDirectory (Join-Path $fixtureRoot 'missing-ownership-install.log')
    if ($missingOwnershipExitCode -eq 0) { throw '缺少所有权清单的非空目录被错误接管' }
    if (-not (Test-Path -LiteralPath (Join-Path $missingOwnershipDirectory 'missing-note.txt'))) {
        throw '缺少所有权清单被拒绝后用户文件没有保留'
    }

    # 清单身份字段齐全但包含通配符时也必须 fail-closed，不能静默跳过损坏行。
    New-Item -ItemType Directory -Path $corruptOwnershipDirectory -Force | Out-Null
    New-Payload $corruptOwnershipDirectory @('NearMate.exe', 'resources', 'corrupt-note.dll')
    Add-Content -LiteralPath (Join-Path $corruptOwnershipDirectory 'release-root-files.txt') -Value '*.dll'
    'corrupt user data' | Set-Content -LiteralPath (Join-Path $corruptOwnershipDirectory 'corrupt-note.txt') -Encoding utf8NoBOM
    $corruptExitCode = Invoke-SmokeInstall $corruptSetup $corruptOwnershipDirectory (Join-Path $fixtureRoot 'corrupt-ownership-install.log')
    if ($corruptExitCode -eq 0) { throw '包含非法通配符的损坏所有权清单被错误接管' }
    if (-not (Test-Path -LiteralPath (Join-Path $corruptOwnershipDirectory 'corrupt-note.txt'))) {
        throw '损坏所有权清单被拒绝后用户文件没有保留'
    }

    Write-Host '发行清理 smoke 通过：首次安装、旧所有权清理、用户文件保留、根目录联接阻断、schema v2、缺少清单和损坏清单拒绝。'
}
finally {
    if (Test-Path -LiteralPath $fixtureRoot) {
        Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
    }
}
