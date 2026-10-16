<#
.SYNOPSIS
在构建阶段准备固定版本的 FFmpeg、ffprobe 和许可说明；客户安装后直接使用随包程序。

.PARAMETER Destination
本次 staging 的 tools/ffmpeg 目录；程序放在其 bin 子目录。

.PARAMETER CacheRoot
保存固定版本 ZIP 的构建缓存目录，已有缓存也必须通过 SHA256 校验。

.OUTPUTS
无；成功后写入两个可执行文件、LICENSE 和包含源码出处的原始 README.txt。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$CacheRoot = (Join-Path $PSScriptRoot '..\.artifacts\runtime-tools\ffmpeg-8.1.2')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build_safety.ps1')
$archiveName = 'ffmpeg-8.1.2-essentials_build.zip'
$archiveUrl = "https://www.gyan.dev/ffmpeg/builds/packages/$archiveName"
$archiveSha256 = 'db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec'
$archivePrefix = 'ffmpeg-8.1.2-essentials_build/'
$expectedFiles = [ordered]@{
    'bin/ffmpeg.exe' = '1326dde4c84ff1f96fe6b8916c5bed29e163e9b5dccf995f6f3db069d143ec5e'
    'bin/ffprobe.exe' = 'b49ccc7c6547b141ad5a2f6ec69cc04323d7133d7704d70b331b904c63eecb07'
    'LICENSE' = '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903'
    'README.txt' = '9172433fb251059a58d2ff11ba8c6132e04819136ed96e809563911ff0d13816'
}
$resolvedDestination = [IO.Path]::GetFullPath($Destination)
$resolvedCacheRoot = [IO.Path]::GetFullPath($CacheRoot)
$archivePath = Join-Path $resolvedCacheRoot $archiveName
Assert-NoReparsePoints $resolvedDestination 'FFmpeg staging 目录'
Assert-NoReparsePointInPath $archivePath 'FFmpeg 归档缓存'

if (-not (Test-Path -LiteralPath $archivePath)) {
    New-Item -ItemType Directory -Path $resolvedCacheRoot -Force | Out-Null
    $downloadPath = Join-Path $resolvedCacheRoot "$archiveName.$([guid]::NewGuid()).part"
    try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $downloadPath -TimeoutSec 180
        if ((Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveSha256) {
            throw 'FFmpeg 官方 Windows 归档 SHA256 不匹配，拒绝打包'
        }
        Move-Item -LiteralPath $downloadPath -Destination $archivePath
    }
    finally {
        if (Test-Path -LiteralPath $downloadPath -PathType Leaf) {
            Remove-Item -LiteralPath $downloadPath -Force
        }
    }
}

# 版本与摘要固定在代码中；缓存损坏时直接终止，避免换源或临时更新把坏包带入发行。
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf) -or
    (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveSha256) {
    throw "FFmpeg 缓存归档 SHA256 不匹配：$archivePath"
}

$archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    foreach ($relativePath in $expectedFiles.Keys) {
        $entryName = "$archivePrefix$relativePath"
        $entries = @($archive.Entries | Where-Object { $_.FullName -ceq $entryName })
        if ($entries.Count -ne 1 -or $entries[0].Length -le 0) {
            throw "FFmpeg 固定归档缺少唯一有效文件：$entryName"
        }
        $stream = $entries[0].Open()
        try {
            $actualDigest = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($stream)).ToLowerInvariant()
        }
        finally { $stream.Dispose() }
        if ($actualDigest -ne $expectedFiles[$relativePath]) {
            throw "FFmpeg 归档文件 SHA256 不匹配：$entryName"
        }
    }

    # 仅按固定清单提取，不采用 ZIP 自带路径递归解压，避免多余程序和越界条目进入安装包。
    foreach ($relativePath in $expectedFiles.Keys) {
        $targetPath = Join-Path $resolvedDestination ($relativePath -replace '/', '\')
        Assert-NoReparsePointInPath $targetPath 'FFmpeg staging 文件'
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
        [IO.Compression.ZipFileExtensions]::ExtractToFile($archive.GetEntry("$archivePrefix$relativePath"), $targetPath, $true)
        if ((Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedFiles[$relativePath]) {
            throw "FFmpeg staging 文件 SHA256 不匹配：$relativePath"
        }
    }
}
finally { $archive.Dispose() }

Write-Host 'FFmpeg 8.1.2、ffprobe、GPL v3 许可及源码出处说明已通过固定摘要校验。'
