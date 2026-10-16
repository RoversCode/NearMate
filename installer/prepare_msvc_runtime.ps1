<#
.SYNOPSIS
把固定版本的官方 C++ 运行库和许可放到随包 Python，避免依赖客户电脑预装的运行库。

.PARAMETER RuntimePath
已经创建的冻结 Python 根目录；DLL 放在实际解释器所在的 Scripts 目录。

.PARAMETER SourceDirectory
官方 Microsoft.VC143.CRT 的 x64 目录。默认读取 NEARMATE_MSVC_RUNTIME_DIRECTORY，
未设置时先查找固定 Visual Studio Redist，再从固定微软公开安装包提取，不使用 System32。

.OUTPUTS
无；源文件、签名、版本或路径不符合固定合同则抛出异常，不写入发行目录。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimePath,
    [string]$SourceDirectory = $env:NEARMATE_MSVC_RUNTIME_DIRECTORY
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build_safety.ps1')
$manifestPath = Join-Path $PSScriptRoot 'msvc-runtime.json'
$licenseSource = Join-Path $PSScriptRoot 'licenses\msvc-runtime'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$resolvedRuntime = [IO.Path]::GetFullPath($RuntimePath)
$scriptsDirectory = Join-Path $resolvedRuntime 'Scripts'
$licenseDestination = Join-Path $resolvedRuntime 'licenses\msvc-runtime'
Assert-NoReparsePointInPath $resolvedRuntime 'C++ 运行库目标 Python 目录'
Assert-NoReparsePoints $scriptsDirectory 'C++ 运行库目标 Scripts 目录'
Assert-NoReparsePoints $licenseDestination 'C++ 运行库目标许可目录'
Assert-NoReparsePoints $licenseSource 'C++ 运行库源许可目录'
Assert-NoReparsePointInPath $manifestPath 'C++ 运行库固定清单'
if (-not (Test-Path -LiteralPath $resolvedRuntime -PathType Container)) {
    throw "冻结 Python 根目录不存在：$resolvedRuntime"
}

if ([string]::IsNullOrWhiteSpace($SourceDirectory)) {
    # 锁定版本而不是挑选“最新版本”，才能在另一台构建机复现同一组 DLL 摘要。
    $programRoots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique
    $candidates = foreach ($programRoot in $programRoots) {
        foreach ($edition in @('BuildTools', 'Community', 'Professional', 'Enterprise')) {
            Join-Path $programRoot "Microsoft Visual Studio\2022\$edition\VC\Redist\MSVC\$($manifest.redist_directory_version)\x64\Microsoft.VC143.CRT"
        }
    }
    $SourceDirectory = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Container } | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($SourceDirectory)) {
        $SourceDirectory = & (Join-Path $PSScriptRoot 'prepare_msvc_download.ps1')
    }
}

$resolvedSource = [IO.Path]::GetFullPath($SourceDirectory)
Assert-NoReparsePoints $resolvedSource 'C++ 运行库官方源目录'
if (-not (Test-Path -LiteralPath $resolvedSource -PathType Container)) {
    throw "官方 C++ 运行库目录不存在：$resolvedSource"
}
foreach ($record in $manifest.files) {
    $sourcePath = Join-Path $resolvedSource $record.name
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "官方 C++ 运行库缺少文件：$($record.name)"
    }
    if ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256) {
        throw "官方 C++ 运行库 SHA256 不匹配：$($record.name)"
    }
    if ([Diagnostics.FileVersionInfo]::GetVersionInfo($sourcePath).FileVersion -ne $manifest.version) {
        throw "官方 C++ 运行库版本不匹配：$($record.name)"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $sourcePath
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.Subject -notmatch '^CN=Microsoft Windows Software Compatibility Publisher, O=Microsoft Corporation,') {
        throw "官方 C++ 运行库 Microsoft 签名无效：$($record.name)"
    }
}
foreach ($record in $manifest.licenses) {
    $licensePath = Join-Path $licenseSource $record.name
    if (-not (Test-Path -LiteralPath $licensePath -PathType Leaf) -or
        (Get-FileHash -LiteralPath $licensePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256) {
        throw "Microsoft C++ 运行库原始许可 SHA256 不匹配：$($record.name)"
    }
}
$noticePath = Join-Path $licenseSource 'SOURCE.txt'
if (-not (Test-Path -LiteralPath $noticePath -PathType Leaf)) {
    throw 'Microsoft C++ 运行库来源说明不存在'
}

# 先把所有源材料核实完整，再覆盖 Python 原有的较旧 VCRUNTIME，避免混合版本。
New-Item -ItemType Directory -Path $scriptsDirectory, $licenseDestination -Force | Out-Null
foreach ($record in $manifest.files) {
    Copy-Item -LiteralPath (Join-Path $resolvedSource $record.name) -Destination (Join-Path $scriptsDirectory $record.name) -Force
}
foreach ($record in $manifest.licenses) {
    Copy-Item -LiteralPath (Join-Path $licenseSource $record.name) -Destination (Join-Path $licenseDestination $record.name) -Force
}
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $licenseDestination 'msvc-runtime.json') -Force
Copy-Item -LiteralPath $noticePath -Destination (Join-Path $licenseDestination 'SOURCE.txt') -Force
Write-Host "官方 Microsoft C++ 运行库 $($manifest.version) 的 10 个 DLL 和原始许可已放入冻结 Python。"
