$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$scanRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-build-safety-$([guid]::NewGuid())"
$outsideRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-build-safety-outside-$([guid]::NewGuid())"

try {
    . (Join-Path $repoRoot 'installer\build_safety.ps1')
    New-Item -ItemType Directory -Path (Join-Path $scanRoot 'nested'), $outsideRoot -Force | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $scanRoot 'nested\external') -Target $outsideRoot | Out-Null

    $reparseRejected = $false
    try {
        Assert-NoReparsePoints $scanRoot '构建安全扫描目录'
    }
    catch {
        $reparseRejected = $_.Exception.Message -match '重解析点'
    }

    if (-not $reparseRejected) {
        throw '目录树内部的 junction 未被构建安全扫描拒绝'
    }
    Write-Host '构建安全递归 reparse 扫描通过。'
}
finally {
    Remove-Item -LiteralPath $scanRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $outsideRoot -Recurse -Force -ErrorAction SilentlyContinue
}
