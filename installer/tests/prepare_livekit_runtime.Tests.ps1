$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-livekit-provision-$([guid]::NewGuid())"
$cacheRoot = Join-Path $fixtureRoot 'cache'
$destination = Join-Path $fixtureRoot 'runtime\tools\livekit'
$linkedDestination = Join-Path $fixtureRoot 'linked-runtime'
$outsideTarget = Join-Path $fixtureRoot 'external'

try {
    New-Item -ItemType Directory -Path $cacheRoot, $outsideTarget -Force | Out-Null
    'not an official archive' | Set-Content -LiteralPath (Join-Path $cacheRoot 'livekit_1.12.0_windows_amd64.zip')
    $corruptArchiveRejected = $false
    try {
        & "$repoRoot\installer\prepare_livekit_runtime.ps1" -Destination $destination -CacheRoot $cacheRoot
    }
    catch {
        $corruptArchiveRejected = $_.Exception.Message -match 'SHA256'
    }
    if (-not $corruptArchiveRejected -or (Test-Path -LiteralPath $destination)) {
        throw '损坏的 LiveKit 缓存必须在创建发行文件之前被拒绝'
    }

    New-Item -ItemType Junction -Path $linkedDestination -Target $outsideTarget | Out-Null
    'keep' | Set-Content -LiteralPath (Join-Path $outsideTarget 'keep.txt')
    $junctionRejected = $false
    try {
        & "$repoRoot\installer\prepare_livekit_runtime.ps1" -Destination $linkedDestination -CacheRoot $cacheRoot
    }
    catch {
        $junctionRejected = $_.Exception.Message -match '重解析点'
    }
    if (-not $junctionRejected -or (Get-ChildItem -LiteralPath $outsideTarget).Count -ne 1) {
        throw 'LiveKit 准备脚本没有在写入外部 junction 目录前拒绝操作'
    }
    Write-Host 'LiveKit 准备回归通过：拒绝损坏缓存、拒绝 junction、不修改外部文件。'
}
finally {
    # 先解除本次创建的 junction，再只清理已经验证属于系统临时目录的测试根。
    if (Test-Path -LiteralPath $linkedDestination) { Remove-Item -LiteralPath $linkedDestination -Force }
    $resolvedFixtureRoot = [IO.Path]::GetFullPath($fixtureRoot)
    $temporaryRootPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedFixtureRoot.StartsWith($temporaryRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'LiveKit 测试清理路径越过临时目录'
    }
    if (Test-Path -LiteralPath $resolvedFixtureRoot) { Remove-Item -LiteralPath $resolvedFixtureRoot -Recurse -Force }
}
