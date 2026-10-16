param([string]$SourceDirectory = $env:NEARMATE_MSVC_RUNTIME_DIRECTORY)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$prepareScript = Join-Path $repoRoot 'installer\prepare_msvc_runtime.ps1'
$manifest = Get-Content -LiteralPath (Join-Path $repoRoot 'installer\msvc-runtime.json') -Raw | ConvertFrom-Json
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-msvc-runtime-test-$([guid]::NewGuid())"
$runtime = Join-Path $fixtureRoot 'python'
$alteredSource = Join-Path $fixtureRoot 'altered-source'
$rejectedRuntime = Join-Path $fixtureRoot 'rejected-python'
$sourceJunction = Join-Path $fixtureRoot 'source-link'
$destinationJunction = Join-Path $fixtureRoot 'target-link'
$outsideTarget = Join-Path $fixtureRoot 'external'
$previousSource = $env:NEARMATE_MSVC_RUNTIME_DIRECTORY

try {
    New-Item -ItemType Directory -Path $runtime, $rejectedRuntime, $outsideTarget -Force | Out-Null
    # 首次使用生产默认定位；校验真实官方签名，不能用 mock 证明发行来源可信。
    & $prepareScript -RuntimePath $runtime -SourceDirectory $SourceDirectory
    foreach ($record in $manifest.files) {
        $copiedPath = Join-Path $runtime "Scripts\$($record.name)"
        if ((Get-FileHash -LiteralPath $copiedPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256) {
            throw "C++ 运行库复制内容不一致：$($record.name)"
        }
    }
    foreach ($record in $manifest.licenses) {
        if ((Get-FileHash -LiteralPath (Join-Path $runtime "licenses\msvc-runtime\$($record.name)") -Algorithm SHA256).Hash.ToLowerInvariant() -ne $record.sha256) {
            throw "原始许可在复制时发生变化：$($record.name)"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $runtime 'licenses\msvc-runtime\SOURCE.txt') -PathType Leaf)) {
        throw 'C++ 运行库来源说明未随包复制'
    }

    # 用已校验的本次副本制造失败场景，失败前不得创建 DLL 或覆盖目标目录的原内容。
    New-Item -ItemType Directory -Path $alteredSource -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $runtime 'Scripts') -File -Filter '*.dll' |
        Copy-Item -Destination $alteredSource
    'keep' | Set-Content -LiteralPath (Join-Path $rejectedRuntime 'keep.txt')
    Remove-Item -LiteralPath (Join-Path $alteredSource 'msvcp140_1.dll')
    $missingRejected = $false
    try { & $prepareScript -RuntimePath $rejectedRuntime -SourceDirectory $alteredSource }
    catch { $missingRejected = $_.Exception.Message -match '缺少文件.*msvcp140_1.dll' }
    if (-not $missingRejected -or (Get-ChildItem -LiteralPath $rejectedRuntime -Force).Count -ne 1) {
        throw '缺少 ORT 直接依赖时必须在写入发行目录前失败'
    }
    Copy-Item -LiteralPath (Join-Path $runtime 'Scripts\msvcp140_1.dll') -Destination $alteredSource
    'replaced DLL' | Set-Content -LiteralPath (Join-Path $alteredSource 'msvcp140.dll')
    $hashRejected = $false
    try { & $prepareScript -RuntimePath $rejectedRuntime -SourceDirectory $alteredSource }
    catch { $hashRejected = $_.Exception.Message -match 'SHA256.*msvcp140.dll' }
    if (-not $hashRejected -or (Get-ChildItem -LiteralPath $rejectedRuntime -Force).Count -ne 1) {
        throw '被替换的同名 C++ DLL 必须在写入发行目录前失败'
    }

    New-Item -ItemType Junction -Path $sourceJunction -Target (Join-Path $runtime 'Scripts') | Out-Null
    New-Item -ItemType Junction -Path $destinationJunction -Target $outsideTarget | Out-Null
    $sourceLinkRejected = $false
    try { & $prepareScript -RuntimePath $rejectedRuntime -SourceDirectory $sourceJunction }
    catch { $sourceLinkRejected = $_.Exception.Message -match '重解析点' }
    $destinationLinkRejected = $false
    try { & $prepareScript -RuntimePath $destinationJunction -SourceDirectory (Join-Path $runtime 'Scripts') }
    catch { $destinationLinkRejected = $_.Exception.Message -match '重解析点' }
    if (-not $sourceLinkRejected -or -not $destinationLinkRejected -or (Get-ChildItem -LiteralPath $outsideTarget -Force).Count -ne 0) {
        throw 'C++ 运行库准备没有在复制前拒绝源或目标 junction'
    }

    $env:NEARMATE_MSVC_RUNTIME_DIRECTORY = Join-Path $runtime 'Scripts'
    $environmentRuntime = Join-Path $fixtureRoot 'environment-python'
    New-Item -ItemType Directory -Path $environmentRuntime | Out-Null
    & $prepareScript -RuntimePath $environmentRuntime
    Write-Host 'C++ 运行库回归通过：官方版本和签名、完整复制、原始许可、环境配置、缺失/篡改及源/目标 junction。'
}
finally {
    if ($null -eq $previousSource) { Remove-Item Env:NEARMATE_MSVC_RUNTIME_DIRECTORY -ErrorAction SilentlyContinue }
    else { $env:NEARMATE_MSVC_RUNTIME_DIRECTORY = $previousSource }
    # 先解除本次创建的联接，再确认清理根属于系统临时目录，不能跟随联接删除其它目录。
    foreach ($junction in @($sourceJunction, $destinationJunction)) {
        if (Test-Path -LiteralPath $junction) { Remove-Item -LiteralPath $junction -Force }
    }
    $resolvedFixture = [IO.Path]::GetFullPath($fixtureRoot)
    $temporaryPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedFixture.StartsWith($temporaryPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $resolvedFixture) -notmatch '^nearmate-msvc-runtime-test-[0-9a-f-]{36}$') {
        throw 'C++ 运行库测试清理路径越过专用临时目录'
    }
    if (Test-Path -LiteralPath $resolvedFixture) { Remove-Item -LiteralPath $resolvedFixture -Recurse -Force }
}
