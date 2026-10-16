$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-runtime-test-$([guid]::NewGuid())"
$targetRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-runtime-target-$([guid]::NewGuid())"
$previousPythonPath = $env:PYTHONPATH
$previousUvEnvironment = $env:UV_PROJECT_ENVIRONMENT
$previousRuntimeProfile = $env:NEARMATE_RUNTIME_PROFILE

# 单独执行生产裁剪函数；小型依赖目录能精确验证同名模块的保留边界，不必重装全部 wheel。
. (Join-Path $repoRoot 'installer\runtime_pruning.ps1')
$pruningFixture = Join-Path ([IO.Path]::GetTempPath()) "nearmate-pruning-$([guid]::NewGuid())"
$sitePackages = Join-Path $pruningFixture 'Lib\site-packages'
$retainedFiles = @(
    'numpy\testing\__init__.py',
    'numpy\testing\_private\utils.py',
    'numba\testing\__init__.py',
    'librosa\__init__.pyi',
    'soundcard\mediafoundation.py.h',
    'nvidia\cudnn\bin\cudnn64_9.dll',
    'scipy\signal\__init__.py',
    'scipy\stats\_page_trend_test.py'
)
$removedFiles = @(
    'numpy\testing\tests\test_utils.py',
    'numpy\tests\__init__.py',
    'numpy\__pycache__\__init__.pyc',
    'scipy\signal\testdata\fixture.txt',
    'nvidia\cudnn\include\cudnn.h',
    'nvidia\cudnn\lib\cudnn.lib',
    'pytest\__init__.py',
    'pytest-8.0.0.dist-info\METADATA',
    '_pytest\__init__.py',
    'wheel\__init__.py',
    'numpy\core\header.h'
)
try {
    foreach ($relativePath in ($retainedFiles + $removedFiles)) {
        $filePath = Join-Path $sitePackages $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $filePath) -Force | Out-Null
        'fixture' | Set-Content -LiteralPath $filePath
    }
    Remove-PythonDevelopmentResources -RuntimePath $pruningFixture
    foreach ($relativePath in $retainedFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $sitePackages $relativePath) -PathType Leaf)) {
            throw "误删运行期依赖：$relativePath"
        }
    }
    foreach ($relativePath in $removedFiles) {
        if (Test-Path -LiteralPath (Join-Path $sitePackages $relativePath)) {
            throw "未移除开发资源：$relativePath"
        }
    }
    Write-Host 'Python 资源裁剪回归通过：保留 testing 运行模块及声明文件，移除测试套件。'
}
finally {
    $temporaryRootPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedPruningFixture = [IO.Path]::GetFullPath($pruningFixture)
    if (-not $resolvedPruningFixture.StartsWith($temporaryRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw '裁剪测试清理路径越过临时目录'
    }
    if (Test-Path -LiteralPath $resolvedPruningFixture) {
        Remove-Item -LiteralPath $resolvedPruningFixture -Recurse -Force
    }
}

try {
    New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
    New-Item -ItemType Junction -Path $testRoot -Target $targetRoot | Out-Null
    $env:PYTHONPATH = 'sentinel-pythonpath'
    $env:UV_PROJECT_ENVIRONMENT = 'sentinel-uv'
    $env:NEARMATE_RUNTIME_PROFILE = 'sentinel-profile'
    $junctionRejected = $false
    try {
        & "$repoRoot\installer\prepare_python_runtime.ps1" -Profile cuda -OutputRoot $testRoot
    }
    catch {
        $junctionRejected = $_.Exception.Message -match '重解析点'
    }
    if (-not $junctionRejected) { throw 'Python 运行时输出根目录 junction 未被拒绝' }
    if ($env:PYTHONPATH -ne 'sentinel-pythonpath' -or
        $env:UV_PROJECT_ENVIRONMENT -ne 'sentinel-uv' -or
        $env:NEARMATE_RUNTIME_PROFILE -ne 'sentinel-profile') {
        throw '输出路径安全检查失败后污染了调用方环境'
    }

    # 让 uv sync 在脚本已经设置 UV_PROJECT_ENVIRONMENT 后失败，验证异常路径也会恢复环境。
    Remove-Item -LiteralPath $testRoot -Force
    Remove-Item -LiteralPath $targetRoot -Recurse -Force
    function uv {
        if ($args[0] -eq 'sync') { $global:LASTEXITCODE = 23; return }
        $global:LASTEXITCODE = 0
    }
    $runtimeFailureRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-runtime-failure-$([guid]::NewGuid())"
    $syncFailed = $false
    try {
        & "$repoRoot\installer\prepare_python_runtime.ps1" -Profile cuda -OutputRoot $runtimeFailureRoot
    }
    catch {
        $syncFailed = $_.Exception.Message -match 'Python 锁定依赖安装失败'
    }
    if (-not $syncFailed -or
        $env:PYTHONPATH -ne 'sentinel-pythonpath' -or
        $env:UV_PROJECT_ENVIRONMENT -ne 'sentinel-uv' -or
        $env:NEARMATE_RUNTIME_PROFILE -ne 'sentinel-profile') {
        throw 'uv sync 失败后污染了调用方环境'
    }
    if (Test-Path -LiteralPath $runtimeFailureRoot) {
        Remove-Item -LiteralPath $runtimeFailureRoot -Recurse -Force
    }
    Write-Host 'Python 运行时输出路径 junction 防护通过。'
}
finally {
    Remove-Item -LiteralPath $testRoot -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $targetRoot) {
        Remove-Item -LiteralPath $targetRoot -Recurse -Force
    }
    if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $previousPythonPath }
    if ($null -eq $previousUvEnvironment) { Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue }
    else { $env:UV_PROJECT_ENVIRONMENT = $previousUvEnvironment }
    if ($null -eq $previousRuntimeProfile) { Remove-Item Env:NEARMATE_RUNTIME_PROFILE -ErrorAction SilentlyContinue }
    else { $env:NEARMATE_RUNTIME_PROFILE = $previousRuntimeProfile }
}
