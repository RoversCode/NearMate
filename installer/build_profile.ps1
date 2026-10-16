[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cuda", "universal")]
    [string]$Profile,
    [string]$OutputRoot = "$PSScriptRoot\staging",
    [string]$ModelCatalog = $env:NEARMATE_MODEL_CATALOG,
    [string]$GitRuntime = $env:NEARMATE_GIT_RUNTIME
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_safety.ps1")
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$stagingRoot = [IO.Path]::GetFullPath((Join-Path $OutputRoot $Profile))
$runtimeRoot = Join-Path $stagingRoot "runtime"
$manifestTemplatePath = Join-Path $PSScriptRoot "runtime-manifest.json"
$manifestTemplate = Get-Content -Raw -LiteralPath $manifestTemplatePath | ConvertFrom-Json

function Get-ManifestRuntimePath([string]$Key) {
    $relative = [string]$manifestTemplate.paths.$Key
    if ([string]::IsNullOrWhiteSpace($relative) -or
        $relative.Contains('\') -or $relative.StartsWith('/') -or
        $relative -match '(^|/)\.\.?(/|$)' -or $relative -match '^[A-Za-z]:') {
        throw "RuntimeLayout paths.$Key 必须是安全的正斜杠相对路径：$relative"
    }
    $resolved = [IO.Path]::GetFullPath((Join-Path $runtimeRoot ($relative -replace '/', '\')))
    $runtimePrefix = $runtimeRoot.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "RuntimeLayout paths.$Key 越过 runtime 根目录：$relative"
    }
    return $resolved
}

$pythonPath = Get-ManifestRuntimePath "python"
$pythonRoot = Split-Path -Parent (Split-Path -Parent $pythonPath)
$backendRoot = Get-ManifestRuntimePath "backend"
$nativeRoot = Get-ManifestRuntimePath "native"
$sharedRoot = Get-ManifestRuntimePath "shared"
$consoleRoot = Get-ManifestRuntimePath "console"
$modelCatalogPath = Get-ManifestRuntimePath "model_catalog"
$seedCharacterCardsRoot = Get-ManifestRuntimePath "seed_character_cards"
$assetsRoot = Get-ManifestRuntimePath "assets"
$livekitServerPath = Get-ManifestRuntimePath "livekit_server"

function Assert-PathExists([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Description 不存在：$Path"
    }
}

function Assert-FileExists([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description 不存在：$Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description 不能是符号链接、目录联接或其它重解析点：$Path"
    }
}

function Copy-RequiredTree([string]$Source, [string]$Destination, [string]$Description) {
    Assert-NoReparsePoints $Source $Description
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
}

function Copy-RequiredFile([string]$Source, [string]$Destination, [string]$Description) {
    Assert-FileExists $Source $Description
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Get-ManifestFileRecords([string]$Root, [string]$ManifestName) {
    Assert-NoReparsePoints $Root "发行 runtime"
    return @(
        foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File -Force | Sort-Object FullName) {
            if ($file.Name -eq $ManifestName -and $file.DirectoryName -eq $Root) {
                continue
            }
            $relative = [IO.Path]::GetRelativePath($Root, $file.FullName).Replace('\', '/')
            [ordered]@{
                path = $relative
                size = [int64]$file.Length
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash.ToLowerInvariant()
            }
        }
    )
}

function Write-RuntimeManifest([string]$RuntimeRoot, [string]$Profile) {
    <#
    .SYNOPSIS
    清理 staging 占位文件，再生成与实际封装资源一致的发行清单。

    .PARAMETER RuntimeRoot
    本次构建的 staging runtime 目录；不会修改复制来源。

    .PARAMETER Profile
    写入清单的 CUDA 或 Universal 发行类型。

    .OUTPUTS
    无；在 RuntimeRoot 中写入 runtime-manifest.json。
    #>
    # Electron 会过滤 .gitkeep；在计算摘要前移除占位文件，避免清单声明包中不存在的资源。
    Assert-NoReparsePoints $RuntimeRoot "发行 runtime"
    Get-ChildItem -LiteralPath $RuntimeRoot -Recurse -File -Force -Filter ".gitkeep" |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

    $manifestPath = Join-Path $RuntimeRoot "runtime-manifest.json"
    $manifest = $manifestTemplate | ConvertTo-Json -Depth 20 | ConvertFrom-Json
    $manifest.profile = $Profile
    $required = @($manifest.required_files | Where-Object { $_ -notlike "native/*.dll" })
    $required += @(
        Get-ChildItem -LiteralPath $nativeRoot -File -Force |
            ForEach-Object { [IO.Path]::GetRelativePath($RuntimeRoot, $_.FullName).Replace('\', '/') }
    )
    $required += @(
        [IO.Path]::GetRelativePath($RuntimeRoot, (Join-Path $pythonRoot "Lib\site-packages\companion\core\main.py")).Replace('\', '/'),
        [IO.Path]::GetRelativePath($RuntimeRoot, (Join-Path $pythonRoot "Lib\site-packages\audio_aec_sidecar\main.py")).Replace('\', '/'),
        [IO.Path]::GetRelativePath($RuntimeRoot, (Join-Path $pythonRoot "Lib\site-packages\audio_aec_poc\main.py")).Replace('\', '/')
    )
    $manifest.required_files = @($required | Select-Object -Unique)
    $manifest | Add-Member -MemberType NoteProperty -Name generated_at -Value (Get-Date).ToUniversalTime().ToString("o") -Force
    $manifest.files = @(Get-ManifestFileRecords $RuntimeRoot "runtime-manifest.json")
    $manifest | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

    foreach ($relative in $manifest.required_files) {
        Assert-FileExists (Join-Path $RuntimeRoot ($relative -replace '/', '\')) "发行清单必需文件"
    }
}

$resolvedOutputRoot = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
if (-not $stagingRoot.StartsWith("$resolvedOutputRoot\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "staging 路径超出指定输出目录：$stagingRoot"
}
Assert-NoReparsePointInPath $resolvedOutputRoot "staging 输出根目录"
Assert-NoReparsePointInPath $stagingRoot "staging profile 目录"
if (Test-Path -LiteralPath $stagingRoot) {
    $existingStagingItem = Get-Item -LiteralPath $stagingRoot -Force
    if (-not $existingStagingItem.PSIsContainer) {
        throw "staging profile 路径不是目录：$stagingRoot"
    }
    Assert-NoReparsePoints $stagingRoot "staging profile 目录"
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

# 先构建前端，再复制只读运行资源；用户数据和模型下载目录不进入 staging。
Push-Location $repoRoot
try {
    pnpm run console:build
    if ($LASTEXITCODE -ne 0) { throw "Console 构建失败" }
    pnpm run desktop:build
    if ($LASTEXITCODE -ne 0) { throw "Desktop 构建失败" }
}
finally {
    Pop-Location
}

Assert-PathExists (Join-Path $repoRoot "desktop\dist-electron") "Desktop 构建产物"
Assert-PathExists (Join-Path $repoRoot "console\dist") "Console 构建产物"
Assert-PathExists (Join-Path $repoRoot "backend\src") "Backend 运行代码"
Assert-PathExists (Join-Path $repoRoot "assets") "发行静态资产"
Assert-FileExists (Join-Path $repoRoot "assets\icon.png") "Desktop 托盘图标"

if ([string]::IsNullOrWhiteSpace($ModelCatalog)) {
    $ModelCatalog = Join-Path $PSScriptRoot "model-catalog.json"
}
$ModelCatalog = [IO.Path]::GetFullPath($ModelCatalog)
Assert-FileExists $ModelCatalog "固定语音模型清单；可用 -ModelCatalog 或 NEARMATE_MODEL_CATALOG 指定"

if ([string]::IsNullOrWhiteSpace($GitRuntime)) {
    $GitRuntime = & (Join-Path $PSScriptRoot "prepare_portable_git.ps1") `
        -OutputRoot (Join-Path $repoRoot ".artifacts\runtime-tools\PortableGit-2.44.0")
}
$GitRuntime = [IO.Path]::GetFullPath($GitRuntime)
Assert-FileExists (Join-Path $GitRuntime "bin\bash.exe") "PortableGit Bash"
Assert-FileExists (Join-Path $GitRuntime "cmd\git.exe") "PortableGit Git"

$pythonSource = Join-Path $repoRoot ".artifacts\python\$Profile"
Assert-PathExists $pythonSource "已冻结的 $Profile Python 运行时"
$sourcePython = Join-Path $pythonSource "Scripts\python.exe"
# 公开 checkout 没有本地模型或 ASR DLL 时，按固定公开来源准备；TTS 权重仍在首次配置下载。
& $sourcePython -B -X utf8 (Join-Path $PSScriptRoot "prepare_realtime_assets.py")
if ($LASTEXITCODE -ne 0) { throw "公开实时模型准备失败" }
& $sourcePython -B -X utf8 (Join-Path $PSScriptRoot "prepare_native_runtime.py")
if ($LASTEXITCODE -ne 0) { throw "公开 ASR 原生库准备失败" }
Copy-RequiredTree $pythonSource $pythonRoot "已冻结的 $Profile Python 运行时"

$sitePackages = Join-Path $pythonRoot "Lib\site-packages"
Assert-PathExists $sitePackages "发行 Python site-packages"

# 安装态只从 site-packages 导入业务包；不把源码目录再复制到 Backend cwd，也不依赖 PYTHONPATH。
Copy-RequiredTree (Join-Path $repoRoot "backend\src\companion") (Join-Path $sitePackages "companion") "Backend Python 包 companion"
foreach ($packageName in @("audio_aec_poc", "audio_aec_sidecar")) {
    Copy-RequiredTree (Join-Path $repoRoot "tools\audio-aec-poc\src\$packageName") (Join-Path $sitePackages $packageName) "AEC Python 包 $packageName"
}
# Background Work 必须在白名单 PATH 中找到固定 Claude、CUA 与 Git Bash，不能借用宿主安装。
$runtimeToolsRoot = Join-Path $runtimeRoot "tools"
Copy-RequiredFile (Join-Path $sitePackages "claude_agent_sdk\_bundled\claude.exe") (Join-Path $runtimeToolsRoot "claude\claude.exe") "Claude Agent SDK 可执行文件"
foreach ($cuaExecutableName in @("cua-driver.exe", "cua-driver-uia.exe")) {
    Copy-RequiredFile (Join-Path $sitePackages "cua_driver\bin\$cuaExecutableName") (Join-Path $runtimeToolsRoot "cua\$cuaExecutableName") "CUA Driver 可执行文件 $cuaExecutableName"
}
Copy-RequiredTree $GitRuntime (Join-Path $runtimeToolsRoot "git") "PortableGit 运行时"

# 媒体 Skill 必须开箱即用；FFmpeg 下载、校验与提取只在构建机发生。
& (Join-Path $PSScriptRoot "prepare_ffmpeg_runtime.ps1") -Destination (Join-Path $runtimeToolsRoot "ffmpeg")

# 媒体服务与安装包一起发行；下载和固定摘要校验只允许发生在构建阶段。
& (Join-Path $PSScriptRoot "prepare_livekit_runtime.ps1") -Destination (Split-Path -Parent $livekitServerPath)
Assert-FileExists $livekitServerPath "LiveKit Server 可执行文件"

# 只复制只读程序、模型和配置；backend/data 运行数据永远不进入安装包。
Copy-RequiredTree (Join-Path $repoRoot "backend\ckpt") (Join-Path $backendRoot "ckpt") "Backend 实时模型资产"
Copy-RequiredTree (Join-Path $repoRoot "backend\assets") (Join-Path $backendRoot "assets") "Backend 受管工作区资产"
# 模型目录只保留运行时会读取的权重和词表，参考脚本、缓存、测试音频与下载归档不进入发行包。
$checkpointRoot = Join-Path $backendRoot "ckpt"
Get-ChildItem -LiteralPath $checkpointRoot -Recurse -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -in @("__pycache__", "test_wavs") } |
    Sort-Object { $_.FullName.Length } -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $checkpointRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -in @(".gitattributes", "README.md", "inference_onnx.py", "run_chinese.sh", "run_multilingual.sh") -or
        $_.Name -like "*.tar.bz2"
    } |
    Remove-Item -Force
New-Item -ItemType Directory -Path (Join-Path $backendRoot "config") -Force | Out-Null
Copy-RequiredFile (Join-Path $PSScriptRoot "release-env.yaml") (Join-Path $backendRoot "config\env.yaml") "发行默认配置"
Copy-RequiredFile (Join-Path $repoRoot "backend\config\realtime_audio.yaml") (Join-Path $backendRoot "config\realtime_audio.yaml") "实时音频配置"
Copy-RequiredTree (Join-Path $repoRoot "assets") $assetsRoot "桌面静态资产"
Copy-RequiredTree (Join-Path $repoRoot "console\dist") $consoleRoot "Console 静态资源"
Copy-RequiredTree (Join-Path $repoRoot "shared") $sharedRoot "共享契约"
Copy-RequiredFile $ModelCatalog $modelCatalogPath "固定语音模型清单"

# 能力文档跟随运行时发布，缺失时构建失败而不是静默使用空能力表。
$streamvoxModelRoot = Join-Path $backendRoot "data\streamvox_model"
New-Item -ItemType Directory -Path $streamvoxModelRoot -Force | Out-Null
foreach ($documentName in @("s2-pro-4b-gguf.md", "voxcpm2-gguf.md")) {
    Copy-RequiredFile (Join-Path $repoRoot "backend\data\streamvox_model\$documentName") (Join-Path $streamvoxModelRoot $documentName) "StreamVox 能力文档 $documentName"
}

# 默认角色预录唤醒音频随包发行；只在 staging 中转换便携文件名和可信内容摘要。
$seedSource = Join-Path $repoRoot "backend\data\character-cards\huohua"
$seedTarget = Join-Path $seedCharacterCardsRoot "huohua"
Copy-RequiredTree $seedSource $seedTarget "默认角色卡素材"
& $pythonPath -I -B (Join-Path $PSScriptRoot "prepare_character_seed.py") $seedSource $seedTarget
if ($LASTEXITCODE -ne 0) { throw "默认角色预录唤醒音频种子转换失败" }

# Qwen ASR 和 StreamVox 的 native DLL 统一归 RuntimeLayout 的 native 目录所有。
New-Item -ItemType Directory -Path $nativeRoot -Force | Out-Null
$nativeSource = Join-Path $repoRoot "backend\bin"
$nativeNames = @("llama.dll", "ggml.dll", "ggml-base.dll", "mtmd.dll", "ggml-rpc.dll", "libomp140.x86_64.dll", "ggml-cpu-x64.dll", "native-manifest.json")
$nativeNames += Get-ChildItem -LiteralPath $nativeSource -File -Filter "ggml-cpu-*.dll" | Select-Object -ExpandProperty Name
$nativeNames += if ($Profile -eq "cuda") { "ggml-cuda.dll" } else { "ggml-vulkan.dll" }
foreach ($nativeName in $nativeNames | Select-Object -Unique) {
    Copy-RequiredFile (Join-Path $nativeSource $nativeName) (Join-Path $nativeRoot $nativeName) "${Profile} native 组件 $nativeName"
}

# StreamVox wheel 为了覆盖开发环境会同时携带 CUDA 与 Vulkan 两套后端；安装包只发行
# 当前 profile 的一套，另一套若留在 site-packages 里会让 Universal/CUDA 边界失真，
# 甚至让动态加载器在错误 GPU 后端上抢先加载。统一在 staging 阶段裁剪未选择的 DLL。
$streamvoxBackendRoot = Join-Path $sitePackages "streamvox\bin"
$unselectedStreamVoxBackend = if ($Profile -eq "cuda") { "ggml-vulkan.dll" } else { "ggml-cuda.dll" }
$unselectedStreamVoxBackendPath = Join-Path $streamvoxBackendRoot $unselectedStreamVoxBackend
if (Test-Path -LiteralPath $unselectedStreamVoxBackendPath -PathType Leaf) {
    Remove-Item -LiteralPath $unselectedStreamVoxBackendPath -Force
}

# 开源发行直接保留业务源码；安装态仍只从随包 site-packages 导入。
$runtimePython = $pythonPath

# 旧冻结运行时或仓库资产可能带着上一次导入产生的缓存；缓存不参与运行，先清掉，
# 否则同一份合法运行时仅因构建机曾经启动过 Python 就会被误判为开发产物。PortableGit
# 自带的 `usr/share/perl5/core_perl/Test` 是 Git 运行时的一部分，不属于 NearMate 代码，
# 不能让它污染下面的开发目录审计。
$runtimeToolsPrefix = (Join-Path $runtimeRoot "tools").TrimEnd('\') + '\'
$cacheDirectories = Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq "__pycache__" -and
        -not $_.FullName.StartsWith($runtimeToolsPrefix, [StringComparison]::OrdinalIgnoreCase)
    } |
    Sort-Object { $_.FullName.Length } -Descending
foreach ($cacheDirectory in $cacheDirectories) {
    Remove-Item -LiteralPath $cacheDirectory.FullName -Recurse -Force
}

$unexpectedDevDirectories = Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object {
        # numpy.testing 等 testing 模块参与 SciPy/语音推理，不能按测试套件拒绝。
        $_.Name -in @("tests", "test", "testdata", "mupdf-devel", ".pytest_cache") -and
        -not $_.FullName.StartsWith($runtimeToolsPrefix, [StringComparison]::OrdinalIgnoreCase)
    }
if ($unexpectedDevDirectories) {
    throw "发行 staging 仍包含测试或开发目录：$($unexpectedDevDirectories.FullName -join ', ')"
}

# Universal 构建必须在产物层面拒绝 CUDA/NVIDIA 依赖，避免误把开发环境 DLL 带进安装程序。
if ($Profile -eq "universal") {
    $cudaMatches = Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "^(onnxruntime[-_]gpu|cudnn|cublas|cufft|curand|nvrtc|nvcuda|ggml-cuda).*\.(dll|pyd|lib)$" -or $_.FullName -match "site-packages[\\/]nvidia[-_].*" -or $_.FullName -match "site-packages[\\/]onnxruntime_gpu" }
    if ($cudaMatches) { throw "Universal staging 检测到 CUDA/NVIDIA 文件：$($cudaMatches.FullName -join ', ')" }
}
else {
    $directmlMatches = Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "^onnxruntime[-_]directml" -or $_.FullName -match "site-packages[\\/]onnxruntime_directml" -or $_.Name -eq "ggml-vulkan.dll" }
    if ($directmlMatches) { throw "CUDA staging 检测到 DirectML/Vulkan 文件：$($directmlMatches.FullName -join ', ')" }
}

# Python 复制到 staging 后再验一次自包含边界，避免 Copy 阶段引入 pyvenv.cfg 或宿主路径。
$previousRuntimeProfile = $env:NEARMATE_RUNTIME_PROFILE
try {
    $env:NEARMATE_RUNTIME_PROFILE = $Profile
    & $runtimePython -I -B -X utf8 (Join-Path $PSScriptRoot "verify_python_runtime.py")
    $pythonRuntimeVerificationExitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $previousRuntimeProfile) { Remove-Item Env:NEARMATE_RUNTIME_PROFILE -ErrorAction SilentlyContinue }
    else { $env:NEARMATE_RUNTIME_PROFILE = $previousRuntimeProfile }
}
if ($pythonRuntimeVerificationExitCode -ne 0) { throw "staging Python 自包含运行时校验失败：$Profile" }

# 先写出清单，再用同一清单和真实 packaged 环境执行启动 smoke。
Write-RuntimeManifest $runtimeRoot $Profile
$smokeDirectory = Join-Path $repoRoot ".artifacts\build-smoke\$Profile-$([guid]::NewGuid())"
& $runtimePython -I -B -X utf8 (Join-Path $PSScriptRoot "verify_backend_startup.py") $runtimeRoot $smokeDirectory
if ($LASTEXITCODE -ne 0) { throw "安装资源首次启动检查失败，日志位于：$smokeDirectory" }

Write-Host "已生成 $Profile staging：$stagingRoot"
