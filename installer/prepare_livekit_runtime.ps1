<#
.SYNOPSIS
在构建机准备固定的 LiveKit 程序和许可文件；客户启动应用时不执行下载。

.PARAMETER Destination
本次 staging 中的 LiveKit 工具目录。

.PARAMETER CacheRoot
保存官方 ZIP 的构建缓存目录；缓存也必须通过固定 SHA256 校验。

.OUTPUTS
无；成功后 Destination 只新增或更新 livekit-server.exe 和 LICENSE。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$CacheRoot = (Join-Path $PSScriptRoot '..\.artifacts\runtime-tools\livekit-1.12.0')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build_safety.ps1')
$archiveName = 'livekit_1.12.0_windows_amd64.zip'
$archiveUrl = "https://github.com/livekit/livekit/releases/download/v1.12.0/$archiveName"
$archiveSha256 = 'b2cb4f5648ffb61f8b73e9c529bec3e6ed3456915b7774a341a3250e620cfb77'
$executableSha256 = '35894ab530ac4d8113a1b791e01d2ad6fc2c3f740be46741f9e6d468035e8705'
$resolvedDestination = [IO.Path]::GetFullPath($Destination)
$resolvedCacheRoot = [IO.Path]::GetFullPath($CacheRoot)
$archivePath = Join-Path $resolvedCacheRoot $archiveName
Assert-NoReparsePoints $resolvedDestination 'LiveKit staging 目录'
Assert-NoReparsePointInPath $archivePath 'LiveKit 归档缓存'

if (-not (Test-Path -LiteralPath $archivePath)) {
    New-Item -ItemType Directory -Path $resolvedCacheRoot -Force | Out-Null
    $downloadPath = Join-Path $resolvedCacheRoot "$archiveName.$([guid]::NewGuid()).part"
    try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $downloadPath -TimeoutSec 180
        if ((Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveSha256) {
            throw 'LiveKit 官方下载归档 SHA256 不匹配，拒绝打包'
        }
        Move-Item -LiteralPath $downloadPath -Destination $archivePath
    }
    finally {
        if (Test-Path -LiteralPath $downloadPath -PathType Leaf) {
            Remove-Item -LiteralPath $downloadPath -Force
        }
    }
}

# 已存在的缓存也不能直接信任；损坏时停止构建，避免网络重试掩盖缓存被替换的原因。
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf) -or
    (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveSha256) {
    throw "LiveKit 缓存归档 SHA256 不匹配：$archivePath"
}

$archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    $expectedNames = @('livekit-server.exe', 'LICENSE')
    if ($archive.Entries.Count -ne 2 -or
        @($archive.Entries | Where-Object { $_.FullName -cnotin $expectedNames }).Count -ne 0) {
        throw 'LiveKit 官方归档内容不符合固定发行合同'
    }
    New-Item -ItemType Directory -Path $resolvedDestination -Force | Out-Null
    foreach ($fileName in $expectedNames) {
        $entry = $archive.GetEntry($fileName)
        if ($null -eq $entry -or $entry.Length -le 0) {
            throw "LiveKit 官方归档缺少有效文件：$fileName"
        }
        # 只提取合同中的两个根文件，不把归档路径直接交给递归解压操作。
        $targetPath = Join-Path $resolvedDestination $fileName
        Assert-NoReparsePointInPath $targetPath 'LiveKit staging 文件'
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $targetPath, $true)
    }
}
finally {
    $archive.Dispose()
}

if ((Get-FileHash -LiteralPath (Join-Path $resolvedDestination 'livekit-server.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $executableSha256) {
    throw 'LiveKit 可执行文件 SHA256 不匹配，拒绝打包'
}
Write-Host 'LiveKit 1.12.0 Windows x64 程序与许可文件已通过固定摘要校验。'
