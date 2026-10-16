[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cuda", "universal")]
    [string]$Profile,
    [string]$PythonVersion = "3.12.13",
    [string]$OutputRoot = "$PSScriptRoot\..\.artifacts\python",
    [string]$MsvcRuntimeDirectory = $env:NEARMATE_MSVC_RUNTIME_DIRECTORY
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_safety.ps1")
. (Join-Path $PSScriptRoot "runtime_pruning.ps1")
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimePath = [IO.Path]::GetFullPath((Join-Path $OutputRoot $Profile))
$pythonPath = Join-Path $runtimePath "Scripts\python.exe"
$extra = if ($Profile -eq "cuda") { "windows-cuda" } else { "windows-universal" }
$previousEnvironment = @{
    PYTHONPATH = $env:PYTHONPATH
    UV_PROJECT_ENVIRONMENT = $env:UV_PROJECT_ENVIRONMENT
    NEARMATE_RUNTIME_PROFILE = $env:NEARMATE_RUNTIME_PROFILE
}

try {

$resolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
if (-not $runtimePath.StartsWith("$resolvedOutputRoot\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "运行时输出目录超出指定目录：$runtimePath"
}

Assert-NoReparsePointInPath $resolvedOutputRoot "Python 运行时输出根目录"
Assert-NoReparsePointInPath $runtimePath "Python 运行时输出目录"
if (Test-Path -LiteralPath $runtimePath) {
    $existingRuntimeItem = Get-Item -LiteralPath $runtimePath -Force
    if (-not $existingRuntimeItem.PSIsContainer) {
        throw "Python 运行时输出路径不是目录：$runtimePath"
    }
    Assert-NoReparsePoints $runtimePath "Python 运行时输出目录"
    Remove-Item -LiteralPath $runtimePath -Recurse -Force
}
New-Item -ItemType Directory -Path (Split-Path -Parent $runtimePath) -Force | Out-Null

Push-Location $repoRoot
try {
    uv venv --python $PythonVersion $runtimePath
    if ($LASTEXITCODE -ne 0) { throw "Python 虚拟环境创建失败" }
    $env:UV_PROJECT_ENVIRONMENT = $runtimePath
    uv sync --locked --no-dev --no-install-project --extra $extra
    if ($LASTEXITCODE -ne 0) { throw "Python 锁定依赖安装失败" }
}
finally {
    Pop-Location
}

# 虚拟环境的启动器依赖开发机的基础 Python。把同版本解释器、标准库和扩展库
# 复制进发行目录，并移除 pyvenv.cfg，运行时才能随安装目录一起搬到其他电脑。
$basePython = & $pythonPath -I -c "import sys; print(sys.base_prefix)"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $basePython 'python312.dll'))) {
    throw "无法定位完整的 Python 3.12 基础运行时：$basePython"
}
foreach ($libraryDirectory in @('DLLs', 'tcl')) {
    Copy-Item -LiteralPath (Join-Path $basePython $libraryDirectory) -Destination (Join-Path $runtimePath $libraryDirectory) -Recurse -Force
}
Get-ChildItem -LiteralPath (Join-Path $basePython 'Lib') -Force |
    Where-Object { $_.Name -ne 'site-packages' } |
    Copy-Item -Destination (Join-Path $runtimePath 'Lib') -Recurse -Force
foreach ($executable in @('python.exe', 'pythonw.exe')) {
    Copy-Item -LiteralPath (Join-Path $basePython $executable) -Destination (Join-Path $runtimePath "Scripts\$executable") -Force
}
Get-ChildItem -LiteralPath $basePython -File -Filter '*.dll' |
    Copy-Item -Destination (Join-Path $runtimePath 'Scripts') -Force
Copy-Item -LiteralPath (Join-Path $basePython 'LICENSE.txt') -Destination (Join-Path $runtimePath 'LICENSE.txt') -Force
Remove-Item -LiteralPath (Join-Path $runtimePath 'pyvenv.cfg') -Force

# Python 自带的 VCRUNTIME 不包含 ORT 所需的完整 C++ 标准库，必须在烟测前补齐同版本官方 DLL。
& (Join-Path $PSScriptRoot 'prepare_msvc_runtime.ps1') -RuntimePath $runtimePath -SourceDirectory $MsvcRuntimeDirectory

Remove-PythonDevelopmentResources -RuntimePath $runtimePath

$runtimeBytes = (Get-ChildItem -LiteralPath $runtimePath -Recurse -File -Force | Measure-Object -Property Length -Sum).Sum
Write-Host ("已清理发行运行时开发资源：{0:N2} GB" -f ($runtimeBytes / 1GB))

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "冻结 Python 解释器不存在：$pythonPath"
}

$env:PYTHONPATH = Join-Path $repoRoot "backend\src"
$pythonVersionOutput = & $pythonPath --version
if ($LASTEXITCODE -ne 0 -or $pythonVersionOutput -notmatch "3\.12\.13") {
    throw "冻结运行时 Python 版本不是 3.12.13：$pythonVersionOutput"
}
$ortPackages = & $pythonPath -c "from importlib.metadata import distributions; print(','.join(sorted({d.metadata['Name'].lower() for d in distributions() if d.metadata['Name'].lower().startswith('onnxruntime')})))"
$torchPackages = & $pythonPath -c "from importlib.metadata import distributions; print(','.join(sorted({d.metadata['Name'].lower() for d in distributions() if d.metadata['Name'].lower() in ('torch','torchaudio','torchvision')})))"
if (-not [string]::IsNullOrWhiteSpace($torchPackages)) {
    throw "发行运行时不允许包含 Torch：$torchPackages"
}
$ortNames = @($ortPackages -split ',' | Where-Object { $_ })
if ($ortNames.Count -ne 1) {
    throw "发行运行时必须且只能包含一个 ONNX Runtime 发行包：$ortPackages"
}
if ($Profile -eq "cuda" -and $ortNames[0] -ne "onnxruntime-gpu") {
    throw "CUDA 运行时必须使用 onnxruntime-gpu：$ortPackages"
}
if ($Profile -eq "universal" -and $ortNames[0] -ne "onnxruntime-directml") {
    throw "Universal 运行时必须使用 onnxruntime-directml：$ortPackages"
}

$smokeImports = @"
import importlib
for module_name in ("numpy", "scipy", "onnxruntime", "streamvox", "streamvox_runtime", "sherpa_onnx", "cryptography"):
    importlib.import_module(module_name)
print("runtime imports ok")
"@
$smokeOutput = & $pythonPath -c $smokeImports
if ($LASTEXITCODE -ne 0 -or $smokeOutput -notmatch "runtime imports ok") {
    throw "发行 Python 运行时导入检查失败：$smokeOutput"
}

$env:NEARMATE_RUNTIME_PROFILE = $Profile
& $pythonPath -I -B (Join-Path $PSScriptRoot 'verify_python_runtime.py')
if ($LASTEXITCODE -ne 0) { throw "Python 自包含运行时校验失败" }

# 导入烟测会按 Python 默认行为生成少量缓存；把它们放在烟测之后清掉，确保
# prepare_python_runtime 的输出本身也能直接作为 staging 输入。
Get-ChildItem -LiteralPath $runtimePath -Recurse -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq "__pycache__" } |
    Remove-Item -Recurse -Force

Write-Host "已准备 $Profile Python 运行时：$runtimePath"
}
finally {
    # 这些变量只服务于本次构建；无论中途成功还是失败，都恢复调用方的原值。
    foreach ($environmentName in $previousEnvironment.Keys) {
        $previousValue = $previousEnvironment[$environmentName]
        if ($null -eq $previousValue) {
            Remove-Item "Env:$environmentName" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$environmentName" $previousValue
        }
    }
}
