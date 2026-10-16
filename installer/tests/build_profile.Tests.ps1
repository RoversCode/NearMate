$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$junctionRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-staging-junction-$([guid]::NewGuid())"
$junctionTarget = Join-Path ([IO.Path]::GetTempPath()) "nearmate-staging-target-$([guid]::NewGuid())"
$markerPath = Join-Path $junctionTarget 'cuda\keep.txt'

try {
    New-Item -ItemType Directory -Path (Split-Path -Parent $markerPath) -Force | Out-Null
    'keep' | Set-Content -LiteralPath $markerPath
    New-Item -ItemType Junction -Path $junctionRoot -Target $junctionTarget | Out-Null

    $junctionRejected = $false
    try {
        & "$repoRoot\installer\build_profile.ps1" `
            -Profile cuda `
            -OutputRoot $junctionRoot `
            -GitRuntime ''
    }
    catch {
        $junctionRejected = $_.Exception.Message -match '重解析点'
    }

    if (-not $junctionRejected) {
        throw 'build_profile 没有拒绝 staging 输出根目录 junction'
    }
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        throw 'build_profile 在拒绝 junction 之前删除了外部目录内容'
    }
    Write-Host 'build_profile staging 路径 junction 防护通过。'
}
finally {
    Remove-Item -LiteralPath $junctionRoot -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $junctionTarget) {
        Remove-Item -LiteralPath $junctionTarget -Recurse -Force
    }
}

# 只加载真实清单函数，在临时 staging 中验证占位文件处理，不执行前端构建或生产资源复制。
. (Join-Path $repoRoot 'installer\build_safety.ps1')
$scriptTokens = $null
$scriptErrors = $null
$buildProfileAst = [Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $repoRoot 'installer\build_profile.ps1'), [ref]$scriptTokens, [ref]$scriptErrors
)
if ($scriptErrors.Count -gt 0) { throw 'build_profile.ps1 存在语法错误' }
foreach ($functionName in @('Assert-FileExists', 'Get-ManifestFileRecords', 'Write-RuntimeManifest')) {
    $functionAst = $buildProfileAst.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
    }, $false)
    if ($null -eq $functionAst) { throw "缺少发行清单函数：$functionName" }
    . ([scriptblock]::Create($functionAst.Extent.Text))
}

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-manifest-test-$([guid]::NewGuid())"
$sourceRoot = Join-Path $fixtureRoot 'source'
$runtimeRoot = Join-Path $fixtureRoot 'runtime'
try {
    New-Item -ItemType Directory -Path (Join-Path $sourceRoot 'backend\ckpt') -Force | Out-Null
    foreach ($relative in @('.gitkeep', 'backend\ckpt\.gitkeep')) {
        '' | Set-Content -LiteralPath (Join-Path $sourceRoot $relative)
    }
    'model payload' | Set-Content -LiteralPath (Join-Path $sourceRoot 'backend\ckpt\model.onnx')
    Copy-Item -LiteralPath $sourceRoot -Destination $runtimeRoot -Recurse -Force
    $hiddenPlaceholder = Get-Item -LiteralPath (Join-Path $runtimeRoot 'backend\ckpt\.gitkeep') -Force
    $hiddenPlaceholder.Attributes = $hiddenPlaceholder.Attributes -bor [IO.FileAttributes]::Hidden

    $pythonRoot = Join-Path $runtimeRoot 'python'
    $nativeRoot = Join-Path $runtimeRoot 'native'
    New-Item -ItemType Directory -Path $nativeRoot -Force | Out-Null
    foreach ($relative in @('companion\core\main.py', 'audio_aec_sidecar\main.py', 'audio_aec_poc\main.py')) {
        $modulePath = Join-Path $pythonRoot "Lib\site-packages\$relative"
        New-Item -ItemType Directory -Path (Split-Path -Parent $modulePath) -Force | Out-Null
        'fixture source' | Set-Content -LiteralPath $modulePath
    }
    $manifestTemplate = [pscustomobject]@{
        profile = 'cuda'
        required_files = @('backend/ckpt/model.onnx')
        files = @()
    }

    Write-RuntimeManifest $runtimeRoot 'cuda'
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $runtimeRoot 'runtime-manifest.json') | ConvertFrom-Json
    if (@(Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Force -Filter '.gitkeep').Count -ne 0) {
        throw '发行 staging 仍包含会被 Electron 过滤的 .gitkeep 占位文件'
    }
    if (@($manifest.files | Where-Object { $_.path -match '(^|/)\.gitkeep$' }).Count -ne 0) {
        throw '发行清单仍收录 .gitkeep 占位文件'
    }
    if ($manifest.files.Count -ne 4 -or $manifest.required_files.Count -ne 4) {
        throw '移除占位文件时误删了真实运行资源或必需文件记录'
    }
    foreach ($record in $manifest.files) {
        $file = Get-Item -LiteralPath (Join-Path $runtimeRoot $record.path)
        if ($file.Length -ne $record.size -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant() -ne $record.sha256) {
            throw "发行清单与实际运行资源不一致：$($record.path)"
        }
    }
    foreach ($relative in @('.gitkeep', 'backend\ckpt\.gitkeep')) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $relative) -PathType Leaf)) {
            throw 'staging 占位文件清理误改了源目录'
        }
    }
    Write-Host '发行清单回归通过：清理 staging 占位文件、保留源目录、核对实际文件摘要。'
}
finally {
    $resolvedFixtureRoot = [IO.Path]::GetFullPath($fixtureRoot)
    $temporaryRootPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedFixtureRoot.StartsWith($temporaryRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '测试清理路径越过临时目录'
    }
    if (Test-Path -LiteralPath $resolvedFixtureRoot) {
        Remove-Item -LiteralPath $resolvedFixtureRoot -Recurse -Force
    }
}
