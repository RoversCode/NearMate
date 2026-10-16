. (Join-Path $PSScriptRoot 'build_safety.ps1')

function Remove-PythonDevelopmentResources {
    <#
    .SYNOPSIS
    只删除发行 Python 不需要的开发资源，保留语音推理实际依赖的模块。
    .PARAMETER RuntimePath
    已安装锁定依赖、且不包含目录联接的 Python 运行时根目录。
    .OUTPUTS
    无；发现不安全路径或无法完成裁剪时抛出异常。
    #>
    param([Parameter(Mandatory = $true)][string]$RuntimePath)

    $runtimeRoot = [IO.Path]::GetFullPath($RuntimePath)
    Assert-NoReparsePointInPath $runtimeRoot 'Python 资源裁剪目录'
    Assert-NoReparsePoints $runtimeRoot 'Python 资源裁剪目录'
    $sitePackages = Join-Path $runtimeRoot 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
        throw "Python site-packages 不存在：$sitePackages"
    }

    # uv 的 --no-dev 只排除项目开发组，传递依赖仍可能带入构建工具。
    foreach ($packageName in @('_pytest', 'pytest', 'build', 'nuitka', 'wheel')) {
        $packagePath = Join-Path $sitePackages $packageName
        if (Test-Path -LiteralPath $packagePath) {
            Remove-Item -LiteralPath $packagePath -Recurse -Force
        }
        Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "$packageName-*.dist-info" |
            Remove-Item -Recurse -Force
    }

    # NVIDIA 的 bin DLL 是运行时必需品，只去掉 include/lib 开发文件。
    foreach ($nvidiaPackage in @('cublas', 'cuda_nvrtc', 'cuda_runtime', 'cudnn', 'cufft', 'curand')) {
        foreach ($developmentDirectory in @('include', 'lib')) {
            $developmentPath = Join-Path $sitePackages "nvidia\$nvidiaPackage\$developmentDirectory"
            if (Test-Path -LiteralPath $developmentPath) {
                Remove-Item -LiteralPath $developmentPath -Recurse -Force
            }
        }
    }

    # testing 是公共支持模块名，不能等同于 tests 测试套件。SciPy 的正常导入
    # 会读取 numpy.testing；删掉它会让语音模型在下载完成后才加载失败。
    $devDirectoryNames = @('__pycache__', 'tests', 'test', 'testdata', 'mupdf-devel')
    Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Directory -Force |
        Sort-Object { $_.FullName.Length } -Descending |
        Where-Object { $devDirectoryNames -contains $_.Name } |
        Remove-Item -Recurse -Force

    # librosa 的 lazy_loader 会在运行期读 .pyi；soundcard 也需要这份 CFFI 声明。
    # Python 文件不能按 *_test 名字删除：scipy.stats._page_trend_test 是统计检验算法。
    $devExtensions = @('.pyx', '.pxd', '.h', '.hpp', '.c', '.cc', '.cpp', '.lib', '.a')
    $runtimeHeaderAllowList = @(
        (Join-Path $sitePackages 'soundcard\mediafoundation.py.h').ToLowerInvariant()
    )
    Get-ChildItem -LiteralPath $sitePackages -Recurse -File -Force |
        Where-Object {
            ($devExtensions -contains $_.Extension.ToLowerInvariant()) -and
                ($runtimeHeaderAllowList -notcontains $_.FullName.ToLowerInvariant())
        } |
        Remove-Item -Force

    $forbiddenRuntimeArtifacts = @('_pytest', 'pytest', 'build', 'nuitka', 'wheel') |
        ForEach-Object { Join-Path $sitePackages $_ }
    $remainingForbidden = @($forbiddenRuntimeArtifacts | Where-Object { Test-Path -LiteralPath $_ })
    if ($remainingForbidden.Count -gt 0) {
        throw "发行运行时仍包含开发包：$($remainingForbidden -join ', ')"
    }
}
