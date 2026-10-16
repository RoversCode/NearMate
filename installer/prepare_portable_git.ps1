<#
.SYNOPSIS
下载并提取固定 PortableGit，给后台工作提供随包 Git 和 Bash。
.PARAMETER OutputRoot
PortableGit 的受管缓存目录，不覆盖调用者的系统 Git。
.OUTPUTS
返回同时包含 cmd/git.exe 和 bin/bash.exe 的 Git 根目录。
#>
[CmdletBinding()]
param([string]$OutputRoot = "$PSScriptRoot\..\.artifacts\runtime-tools\PortableGit-2.44.0")
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build_safety.ps1')
$gitRoot = [IO.Path]::GetFullPath($OutputRoot)
Assert-NoReparsePointInPath $gitRoot 'PortableGit 缓存目录'
$archive = $gitRoot + '.7z.exe'
New-Item -ItemType Directory -Path (Split-Path -Parent $gitRoot) -Force | Out-Null
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    Write-Host '下载固定 PortableGit 2.44.0。'
    Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.44.0.windows.1/PortableGit-2.44.0-64-bit.7z.exe' -OutFile $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne '1fc64ca91b9b475ab0ada72c9f7b3addbe69a6c8f520be31425cf21841cca369') {
    throw 'PortableGit 固定归档 SHA256 不匹配'
}
# 自解压程序只展开到受管缓存，不安装或改动 PATH；后续 staging 再校验关键程序。
if (-not (Test-Path -LiteralPath (Join-Path $gitRoot 'cmd/git.exe')) -or
    -not (Test-Path -LiteralPath (Join-Path $gitRoot 'bin/bash.exe'))) {
    New-Item -ItemType Directory -Path $gitRoot -Force | Out-Null
    $extractor = Start-Process -FilePath $archive -ArgumentList @('-y', ('-o"' + $gitRoot + '"')) -WindowStyle Hidden -PassThru
    $extractor.WaitForExit()
    if ($extractor.ExitCode -ne 0) { throw "PortableGit 提取失败：$($extractor.ExitCode)" }
}
Assert-NoReparsePoints $gitRoot 'PortableGit 固定运行库'
foreach ($relative in @('cmd/git.exe','bin/bash.exe','LICENSE.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $gitRoot $relative) -PathType Leaf)) { throw "PortableGit 缺少 $relative" }
}
$version = & (Join-Path $gitRoot 'cmd/git.exe') --version
if ($LASTEXITCODE -ne 0 -or $version -ne 'git version 2.44.0.windows.1') { throw 'PortableGit 实际版本不符合固定发行' }
Write-Output $gitRoot
