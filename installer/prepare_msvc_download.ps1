<#
.SYNOPSIS
从固定的微软公开安装包提取 C++ DLL，不安装或修改系统运行库。
.PARAMETER OutputRoot
下载、CAB 中间文件及最终 DLL 的受管缓存根目录。
.OUTPUTS
返回通过固定 SHA256 校验的十个 DLL 所在目录。
#>
[CmdletBinding()]
param([string]$OutputRoot = "$PSScriptRoot\..\.artifacts\runtime-tools\msvc-14.42.34433")
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build_safety.ps1')
$cacheRoot = [IO.Path]::GetFullPath($OutputRoot)
Assert-NoReparsePointInPath $cacheRoot 'C++ 运行库下载缓存'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'msvc-runtime.json') -Raw | ConvertFrom-Json
$destination = Join-Path $cacheRoot 'dll'
New-Item -ItemType Directory -Path $cacheRoot, $destination -Force | Out-Null
Assert-NoReparsePoints $cacheRoot 'C++ 运行库缓存'
$complete = $true
foreach ($record in $manifest.files) {
    $candidate = Join-Path $destination $record.name
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf) -or
        (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256) {
        $complete = $false
        break
    }
}
if ($complete) { Write-Output $destination; return }
$archive = Join-Path $cacheRoot 'VC_redist.x64.exe'
$expectedHash = '1821577409c35b2b9505ac833e246376cc68a8262972100444010b57226f0940'
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Write-Host '下载固定 Microsoft C++ Runtime 14.42.34433.0。'
    Invoke-WebRequest -Uri 'https://download.visualstudio.microsoft.com/download/pr/c7dac50a-e3e8-40f6-bbb2-9cc4e3dfcabe/1821577409C35B2B9505AC833E246376CC68A8262972100444010B57226F0940/VC_redist.x64.exe' -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
    throw 'Microsoft C++ 运行库安装包 SHA256 不匹配'
}
# Burn 安装器包含完整 CAB；只提取归档，绝不执行安装器或从系统目录拼 DLL。
$archiveBytes = [IO.File]::ReadAllBytes($archive)
$archiveText = [Text.Encoding]::GetEncoding(28591).GetString($archiveBytes)
$cabinets = Join-Path $cacheRoot 'cabinets'
New-Item -ItemType Directory -Path $cabinets -Force | Out-Null
$offset = 0
$cabinetNumber = 0
while (($offset = $archiveText.IndexOf('MSCF', $offset, [StringComparison]::Ordinal)) -ge 0) {
    $cabinetStart = $offset
    $offset += 4
    if ($cabinetStart + 36 -gt $archiveBytes.Length) { continue }
    $cabinetSize = [BitConverter]::ToUInt32($archiveBytes, $cabinetStart + 8)
    $fileTableOffset = [BitConverter]::ToUInt32($archiveBytes, $cabinetStart + 16)
    if ($cabinetSize -lt 36 -or $cabinetStart + $cabinetSize -gt $archiveBytes.Length -or
        $fileTableOffset -lt 36 -or $fileTableOffset -ge $cabinetSize -or
        $archiveBytes[$cabinetStart + 25] -ne 1 -or $archiveBytes[$cabinetStart + 24] -ne 3) { continue }
    $cabinetNumber++
    $cabinetPath = Join-Path $cabinets "outer-$cabinetNumber.cab"
    $cabinetBytes = [byte[]]::new($cabinetSize)
    [Array]::Copy($archiveBytes, $cabinetStart, $cabinetBytes, 0, $cabinetSize)
    [IO.File]::WriteAllBytes($cabinetPath, $cabinetBytes)
    $expandedPath = Join-Path $cabinets "outer-$cabinetNumber"
    New-Item -ItemType Directory -Path $expandedPath -Force | Out-Null
    & expand.exe '-F:*' $cabinetPath $expandedPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法提取 Microsoft C++ 外层 CAB' }
}
if ($cabinetNumber -eq 0) { throw '固定 Microsoft 安装包未包含有效 CAB' }
# 内层 CAB 名称由微软打包器生成；按文件头展开，再按摘要选择所需 x64 DLL。
foreach ($payload in @(Get-ChildItem -LiteralPath $cabinets -Recurse -File | Where-Object { $_.Extension -ne '.cab' })) {
    $payloadBytes = [IO.File]::ReadAllBytes($payload.FullName)
    if ($payloadBytes.Length -lt 4 -or [Text.Encoding]::ASCII.GetString($payloadBytes, 0, 4) -ne 'MSCF') { continue }
    $expandedPath = Join-Path $payload.DirectoryName ($payload.Name + '-expanded')
    New-Item -ItemType Directory -Path $expandedPath -Force | Out-Null
    & expand.exe '-F:*' $payload.FullName $expandedPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法提取 Microsoft C++ 内层 CAB' }
}
$filesByHash = @{}
foreach ($file in Get-ChildItem -LiteralPath $cabinets -Recurse -File) {
    $filesByHash[(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()] = $file.FullName
}
foreach ($record in $manifest.files) {
    $source = $filesByHash[$record.sha256]
    if (-not $source) { throw "微软公开安装包缺少固定 DLL：$($record.name)" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $destination $record.name) -Force
}
Write-Output $destination
