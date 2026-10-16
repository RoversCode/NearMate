[CmdletBinding()]
param(
    [ValidateSet("all", "cuda", "universal")]
    [string]$Profile = "all",
    [string]$StreamVoxWheel = "",
    [string]$ModelCatalog = $env:NEARMATE_MODEL_CATALOG,
    [string]$GitRuntime = $env:NEARMATE_GIT_RUNTIME,
    [switch]$SkipNodeInstall,
    [string]$Iscc = "",
    [string]$OutputRoot = "$PSScriptRoot\Output",
    [char]$DriveLetter = "N"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_safety.ps1")
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$profiles = if ($Profile -eq "all") { @("cuda", "universal") } else { @($Profile) }
# 环境变量未定义时也传递显式空值，避免 PowerShell 丢弃 null 参数后错位绑定后续参数。
if ($null -eq $ModelCatalog) { $ModelCatalog = "" }
if ($null -eq $GitRuntime) { $GitRuntime = "" }
$expectedStreamVoxName = "streamvox-2.0.0-cp312-none-win_amd64.whl"
$expectedStreamVoxSha256 = "E6D52851088DE43B08E6F9A000DBEDBC15259089031022E8DFBCD789636B4BA3"
$expectedStreamVoxSize = 242814050
$streamVoxUrl = "https://github.com/RoversCode/StreamVox/releases/download/v2.0.0/$expectedStreamVoxName"
$wheelRoot = Join-Path $repoRoot ".artifacts\wheels"

function Assert-File([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description 不存在：$Path"
    }
}

function Invoke-Checked([string]$Command, [string[]]$Arguments) {
    Write-Host "==> $Command $($Arguments -join ' ')"
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令失败（退出码 $LASTEXITCODE）：$Command $($Arguments -join ' ')"
    }
}

function Prepare-FixedWheel(
    [string]$InputPath,
    [string]$ExpectedName,
    [string]$ExpectedSha256,
    [int64]$ExpectedSize,
    [string]$DownloadUrl,
    [string]$Description
) {
    <#
    .SYNOPSIS
    下载或复用公开 wheel，校验固定大小与摘要后放进本仓库的独立构建缓存。

    .PARAMETER InputPath
    可选的既有 wheel 路径；指定时只校验和复制，不访问网络。

    .PARAMETER ExpectedName
    公开发行固定的 wheel 文件名。

    .PARAMETER ExpectedSha256
    公开发行固定的 SHA256 摘要。

    .PARAMETER ExpectedSize
    公开发行固定的文件字节数。

    .PARAMETER DownloadUrl
    未提供本地 wheel 且缓存缺失时使用的公开下载地址。

    .PARAMETER Description
    失败诊断中使用的发行物名称。

    .OUTPUTS
    无；验证通过后在 wheelRoot 中保留固定文件名的 wheel。
    #>
    Assert-NoReparsePointInPath $wheelRoot "wheel 缓存目录"
    $fixedPath = Join-Path $wheelRoot $ExpectedName
    $downloadPath = $null
    if ([string]::IsNullOrWhiteSpace($InputPath)) {
        $InputPath = $fixedPath
        if (-not (Test-Path -LiteralPath $fixedPath)) {
            New-Item -ItemType Directory -Path $wheelRoot -Force | Out-Null
            $downloadPath = "$fixedPath.download-$([guid]::NewGuid())"
            # 先写临时文件，只有完整下载且验证通过才进入固定缓存，断网不会留下可被误用的 wheel。
            try {
                Invoke-WebRequest -Uri $DownloadUrl -OutFile $downloadPath
            }
            catch {
                if (Test-Path -LiteralPath $downloadPath) {
                    Remove-Item -LiteralPath $downloadPath -Force
                }
                throw
            }
            $InputPath = $downloadPath
        }
    }
    $InputPath = [IO.Path]::GetFullPath($InputPath)
    try {
        Assert-NoReparsePointInPath $InputPath $Description
        Assert-File $InputPath $Description
        $actualSize = (Get-Item -LiteralPath $InputPath).Length
        if ($actualSize -ne $ExpectedSize) {
            throw "$Description 文件大小不匹配。期望 $ExpectedSize，实际 ${actualSize}：$InputPath"
        }
        $actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $InputPath).Hash.ToUpperInvariant()
        if ($actualSha256 -ne $ExpectedSha256) {
            throw "$Description SHA256 不匹配。期望 $ExpectedSha256，实际 ${actualSha256}：$InputPath"
        }

        Assert-NoReparsePointInPath $fixedPath "固定 wheel 缓存"
        if ($InputPath -ne [IO.Path]::GetFullPath($fixedPath)) {
            New-Item -ItemType Directory -Path $wheelRoot -Force | Out-Null
            Copy-Item -LiteralPath $InputPath -Destination $fixedPath -Force
        }
        Write-Host "已验证 $Description：$fixedPath"
    }
    finally {
        if ($null -ne $downloadPath -and (Test-Path -LiteralPath $downloadPath)) {
            Remove-Item -LiteralPath $downloadPath -Force
        }
    }
}

function Get-CurrentInstallerArtifacts([string]$Path, [string]$CurrentProfile) {
    <#
    .SYNOPSIS
    只返回 compile_setup 本次清理后生成的安装程序和分卷。

    .PARAMETER Path
    当前 profile 的安装输出目录。

    .PARAMETER CurrentProfile
    输出文件名中的 profile 名称。

    .OUTPUTS
    安装程序和按编号排序的 .bin 分卷文件对象。
    #>
    $installerName = "NearMate-$CurrentProfile-setup.exe"
    $installerPath = Join-Path $Path $installerName
    Assert-File $installerPath "$CurrentProfile 安装程序"
    $installer = Get-Item -LiteralPath $installerPath -Force
    if ($installer.Length -le 0) {
        throw "$CurrentProfile 安装程序为空：$installerPath"
    }

    $binPattern = "^NearMate-$([regex]::Escape($CurrentProfile))-setup-(\d+)\.bin$"
    $packagedFiles = @(Get-ChildItem -LiteralPath $Path -File | Where-Object { $_.Extension -in @('.exe', '.bin') })
    $binFiles = @($packagedFiles | Where-Object { $_.Name -match $binPattern } | Sort-Object {
        [int]([regex]::Match($_.Name, $binPattern).Groups[1].Value)
    })
    if ($binFiles.Count -eq 0) {
        throw "$CurrentProfile 安装程序没有 .bin 分卷：$Path"
    }
    $expectedNumbers = @(1..$binFiles.Count)
    $actualNumbers = @($binFiles | ForEach-Object { [int]([regex]::Match($_.Name, $binPattern).Groups[1].Value) })
    if (($actualNumbers -join ',') -ne ($expectedNumbers -join ',')) {
        throw "$CurrentProfile 安装程序分卷编号不连续：$($actualNumbers -join ',')"
    }
    foreach ($file in $binFiles) {
        if ($file.Length -le 0) {
            throw "$currentProfile 安装程序分卷为空：$($file.FullName)"
        }
    }

    $unexpectedFiles = @($packagedFiles | Where-Object {
        $_.Name -ne $installerName -and $_.Name -notmatch $binPattern
    })
    if ($unexpectedFiles.Count -gt 0) {
        throw "$CurrentProfile 安装输出包含非本次构建的 exe/bin：$($unexpectedFiles.Name -join ', ')"
    }
    return @($installer) + $binFiles
}

if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "找不到 pnpm，请先安装项目要求的 pnpm 9.12.2。"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "找不到 uv，请先安装 uv。"
}

# NearMate 只消费公开固定 wheel；缺少本地缓存时下载发行物，不进入上游仓库或触发编译。
Prepare-FixedWheel $StreamVoxWheel $expectedStreamVoxName $expectedStreamVoxSha256 $expectedStreamVoxSize $streamVoxUrl "StreamVox 2.0.0 wheel"

if (-not $SkipNodeInstall) {
    Push-Location $repoRoot
    try { Invoke-Checked "pnpm" @("install", "--frozen-lockfile") }
    finally { Pop-Location }
}

foreach ($currentProfile in $profiles) {
    Write-Host "`n===== 开始构建 NearMate $currentProfile =====" -ForegroundColor Cyan
    Invoke-Checked "pwsh" @(
        "-NoProfile", "-File", (Join-Path $PSScriptRoot "prepare_python_runtime.ps1"),
        "-Profile", $currentProfile
    )
    Invoke-Checked "pwsh" @(
        "-NoProfile", "-File", (Join-Path $PSScriptRoot "build_profile.ps1"),
        "-Profile", $currentProfile,
        "-ModelCatalog", $ModelCatalog,
        "-GitRuntime", $GitRuntime
    )
    Push-Location $repoRoot
    try { Invoke-Checked "pnpm" @("run", "installer:package:$currentProfile") }
    finally { Pop-Location }

    $compileArguments = @(
        "-NoProfile", "-File", (Join-Path $PSScriptRoot "compile_setup.ps1"),
        "-Profile", $currentProfile,
        "-OutputRoot", $OutputRoot,
        "-DriveLetter", $DriveLetter
    )
    if (-not [string]::IsNullOrWhiteSpace($Iscc)) {
        $compileArguments += @("-Iscc", $Iscc)
    }
    Invoke-Checked "pwsh" $compileArguments

    $releaseDirectory = Join-Path $OutputRoot $currentProfile
    $releaseFiles = @(Get-CurrentInstallerArtifacts $releaseDirectory $currentProfile | Sort-Object Name)
    $checksums = foreach ($file in $releaseFiles) {
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
        "$digest  $($file.Name)"
        Write-Host "已生成：$($file.Name)（$($file.Length) bytes）"
    }
    $checksums | Set-Content -LiteralPath (Join-Path $releaseDirectory "SHA256SUMS.txt") -Encoding utf8
    $runtimePython = Join-Path $repoRoot ".artifacts\python\$currentProfile\Scripts\python.exe"
    $dependencies = & $runtimePython -I -B -X utf8 -c "import importlib.metadata as m,json; print(json.dumps(sorted([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()], key=lambda item:item['name'].lower()), indent=2))"
    if ($LASTEXITCODE -ne 0) { throw "依赖清单生成失败：$currentProfile" }
    $dependencies | Set-Content -LiteralPath (Join-Path $releaseDirectory 'dependencies.json') -Encoding utf8
    @"
NearMate 0.2.0 Windows x64 安装包（$currentProfile）

请完整保留本目录中的安装程序与所有 .bin 分卷，运行 NearMate-$currentProfile-setup.exe。
请使用默认安装目录或较短路径，避免 Windows 传统路径长度限制。
首次启动时配置模型服务，准备语音并完成试听。
NearMate 与内置 StreamVox 免费使用；模型服务凭据由用户自行配置。
SHA256SUMS.txt 用于核对安装程序与分卷；dependencies.json 记录实际依赖版本。
"@ | Set-Content -LiteralPath (Join-Path $releaseDirectory 'README.txt') -Encoding utf8
    Write-Host "请完整复制安装目录：$releaseDirectory"
}
Write-Host "`nNearMate 打包完成：$($profiles -join ', ')" -ForegroundColor Green
