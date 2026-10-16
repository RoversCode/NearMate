$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-ffmpeg-provision-$([guid]::NewGuid())"
$cacheRoot = Join-Path $fixtureRoot 'cache'
$destination = Join-Path $fixtureRoot 'runtime\tools\ffmpeg'
$linkedDestination = Join-Path $fixtureRoot 'linked-runtime'
$outsideTarget = Join-Path $fixtureRoot 'external'

try {
    New-Item -ItemType Directory -Path $cacheRoot, $outsideTarget -Force | Out-Null
    'not the fixed archive' | Set-Content -LiteralPath (Join-Path $cacheRoot 'ffmpeg-8.1.2-essentials_build.zip')
    $corruptArchiveRejected = $false
    try {
        & "$repoRoot\installer\prepare_ffmpeg_runtime.ps1" -Destination $destination -CacheRoot $cacheRoot
    }
    catch { $corruptArchiveRejected = $_.Exception.Message -match 'SHA256' }
    if (-not $corruptArchiveRejected -or (Test-Path -LiteralPath $destination)) {
        throw '损坏的 FFmpeg 缓存必须在写入任何安装资源前被拒绝'
    }

    New-Item -ItemType Junction -Path $linkedDestination -Target $outsideTarget | Out-Null
    'keep' | Set-Content -LiteralPath (Join-Path $outsideTarget 'keep.txt')
    $junctionRejected = $false
    try {
        & "$repoRoot\installer\prepare_ffmpeg_runtime.ps1" -Destination $linkedDestination -CacheRoot $cacheRoot
    }
    catch { $junctionRejected = $_.Exception.Message -match '重解析点' }
    if (-not $junctionRejected -or (Get-ChildItem -LiteralPath $outsideTarget).Count -ne 1) {
        throw 'FFmpeg 准备脚本必须拒绝 junction，不能写入外部目录'
    }

    # 使用真实固定缓存验收完整程序，不依赖本机 PATH 中已经安装的 FFmpeg。
    & "$repoRoot\installer\prepare_ffmpeg_runtime.ps1" -Destination $destination
    $expectedFiles = @('bin\ffmpeg.exe', 'bin\ffprobe.exe', 'LICENSE', 'README.txt')
    if (@(Get-ChildItem -LiteralPath $destination -Recurse -File).Count -ne $expectedFiles.Count) {
        throw 'FFmpeg staging 包含缺失或多余文件'
    }
    foreach ($relative in $expectedFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $destination $relative) -PathType Leaf)) {
            throw "FFmpeg staging 缺少必需文件：$relative"
        }
    }
    $readme = Get-Content -LiteralPath (Join-Path $destination 'README.txt') -Raw
    if ($readme -notmatch 'License: GPL v3' -or $readme -notmatch 'Source Code: https://github.com/FFmpeg/FFmpeg/commit/') {
        throw '随包说明缺少许可或对应源码出处'
    }
    $wavPath = Join-Path $fixtureRoot 'tone.wav'
    & (Join-Path $destination 'bin\ffmpeg.exe') -hide_banner -loglevel error -f lavfi -i 'sine=frequency=440:duration=0.1' -ar 16000 -ac 1 $wavPath
    if ($LASTEXITCODE -ne 0) { throw '随包 FFmpeg 无法生成真实音频' }
    $probeJson = & (Join-Path $destination 'bin\ffprobe.exe') -v error -show_streams -of json $wavPath
    if ($LASTEXITCODE -ne 0) { throw '随包 ffprobe 无法读取真实音频' }
    $probe = ($probeJson -join "`n") | ConvertFrom-Json
    if ($probe.streams[0].sample_rate -ne '16000' -or $probe.streams[0].channels -ne 1 -or
        [double]$probe.streams[0].duration -le 0) {
        throw '随包 FFmpeg/ffprobe 音频输出不符合预期'
    }
    Write-Host 'FFmpeg 发行回归通过：拒绝坏缓存和 junction、四文件完整、许可与来源完整、真实音频可写可读。'
}
finally {
    # 先解除本测试的 junction，再检查绝对路径；只删除独占临时根，保留构建缓存和真实安装。
    if (Test-Path -LiteralPath $linkedDestination) { Remove-Item -LiteralPath $linkedDestination -Force }
    $resolvedFixtureRoot = [IO.Path]::GetFullPath($fixtureRoot)
    $temporaryRootPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedFixtureRoot.StartsWith($temporaryRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'FFmpeg 测试清理路径越过临时目录'
    }
    # Windows 可能在子进程已退出后短暂保留 EXE 扫描句柄；只对这个独占临时根有界重试。
    for ($cleanupAttempt = 0; $cleanupAttempt -lt 5; $cleanupAttempt++) {
        if (-not (Test-Path -LiteralPath $resolvedFixtureRoot)) { break }
        try {
            Remove-Item -LiteralPath $resolvedFixtureRoot -Recurse -Force
            break
        }
        catch [IO.IOException] {
            if ($cleanupAttempt -eq 4) { throw }
            Start-Sleep -Milliseconds 200
        }
    }
}
