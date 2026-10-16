$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $repoRoot 'installer\build_safety.ps1')

# 只执行生产 wheel 准备函数，避免回归测试触发完整双 profile 构建。
$scriptTokens = $null
$scriptErrors = $null
$buildAst = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repoRoot 'installer\build_all.ps1'), [ref]$scriptTokens, [ref]$scriptErrors
)
if ($scriptErrors.Count -gt 0) { throw 'build_all.ps1 存在语法错误' }
foreach ($functionName in @('Assert-File', 'Prepare-FixedWheel')) {
    $functionAst = $buildAst.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
    }, $false)
    if ($null -eq $functionAst) { throw "缺少 wheel 准备函数：$functionName" }
    . ([scriptblock]::Create($functionAst.Extent.Text))
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-wheel-test-$([guid]::NewGuid())"
$wheelRoot = Join-Path $fixtureRoot 'cache'
$payload = [Text.Encoding]::UTF8.GetBytes('fixed public wheel fixture')
$sourcePath = Join-Path $fixtureRoot 'source.whl'
$expectedName = 'fixture.whl'
$downloadCount = 0
$failDownload = $false
function Invoke-WebRequest([string]$Uri, [string]$OutFile) {
    $script:downloadCount += 1
    [IO.File]::WriteAllBytes($OutFile, $script:payload)
    if ($script:failDownload) { throw '模拟下载中断' }
}

try {
    New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
    [IO.File]::WriteAllBytes($sourcePath, $payload)
    $expectedHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
    $fixedPath = Join-Path $wheelRoot $expectedName

    Prepare-FixedWheel '' $expectedName $expectedHash $payload.Length 'https://example.invalid/wheel' '公开 wheel fixture'
    Prepare-FixedWheel '' $expectedName $expectedHash $payload.Length 'https://example.invalid/wheel' '公开 wheel fixture'
    if ($downloadCount -ne 1 -or
        (Get-FileHash -LiteralPath $fixedPath -Algorithm SHA256).Hash -ne $expectedHash) {
        throw '公开 wheel 没有在首次下载校验后复用本地缓存'
    }
    Remove-Item -LiteralPath $fixedPath -Force

    Prepare-FixedWheel $sourcePath $expectedName $expectedHash $payload.Length 'https://example.invalid/wheel' '公开 wheel fixture'
    if ($downloadCount -ne 1) { throw '显式本地 wheel 不应该访问网络' }
    Remove-Item -LiteralPath $fixedPath -Force

    # 下载结果的大小与摘要都必须匹配；任何失败都不能留下固定缓存或临时残片。
    foreach ($failure in @('size', 'hash', 'interrupted')) {
        $failDownload = $failure -eq 'interrupted'
        $size = if ($failure -eq 'size') { $payload.Length + 1 } else { $payload.Length }
        $hash = if ($failure -eq 'hash') { '0' * 64 } else { $expectedHash }
        $rejected = $false
        try {
            Prepare-FixedWheel '' $expectedName $hash $size 'https://example.invalid/wheel' '公开 wheel fixture'
        }
        catch {
            $rejected = $_.Exception.Message -match '不匹配|模拟下载中断'
        }
        if (-not $rejected -or @(Get-ChildItem -LiteralPath $wheelRoot -File -Force).Count -ne 0) {
            throw "失败的公开 wheel 下载仍留下可误用的缓存：$failure"
        }
    }
    $failDownload = $false

    $junctionPath = Join-Path $fixtureRoot 'source-junction'
    New-Item -ItemType Junction -Path $junctionPath -Target $fixtureRoot | Out-Null
    try {
        $rejected = $false
        try {
            Prepare-FixedWheel (Join-Path $junctionPath 'source.whl') $expectedName $expectedHash $payload.Length '' '公开 wheel fixture'
        }
        catch { $rejected = $_.Exception.Message -match '重解析点' }
        if (-not $rejected -or (Test-Path -LiteralPath $fixedPath)) {
            throw '公开 wheel 输入没有拒绝重解析路径'
        }
    }
    finally { Remove-Item -LiteralPath $junctionPath -Force }
    Write-Host '公开 wheel 回归通过：首次下载、缓存复用、本地输入、大小/摘要校验、失败清理与重解析路径防护。'
}
finally {
    $temporaryRootPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedFixtureRoot = [IO.Path]::GetFullPath($fixtureRoot)
    if (-not $resolvedFixtureRoot.StartsWith($temporaryRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'wheel 回归清理路径越过临时目录'
    }
    Assert-NoReparsePoints $resolvedFixtureRoot 'wheel 回归临时目录'
    if (Test-Path -LiteralPath $resolvedFixtureRoot) {
        Remove-Item -LiteralPath $resolvedFixtureRoot -Recurse -Force
    }
}
