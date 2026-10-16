$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$repoRoot = Join-Path ([IO.Path]::GetTempPath()) "nearmate-compile-test-$([guid]::NewGuid())"
$tempInstaller = Join-Path $repoRoot 'installer'
$tempPackage = Join-Path $repoRoot 'desktop\dist-package\cuda-unpacked\win-unpacked'
New-Item -ItemType Directory -Path $tempInstaller,$tempPackage -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot 'installer\compile_setup.ps1') -Destination $tempInstaller -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'installer\build_safety.ps1') -Destination $tempInstaller -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'installer\NearMate.iss') -Destination $tempInstaller -Force
'fake package' | Set-Content -LiteralPath (Join-Path $tempPackage 'NearMate.exe')
New-Item -ItemType Directory -Path (Join-Path $tempPackage 'resources') -Force | Out-Null
'fake icon' | Set-Content -LiteralPath (Join-Path $tempPackage 'resources\icon.ico')
New-Item -ItemType Directory -Path (Join-Path $repoRoot 'desktop\dist-brand') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $tempPackage 'resources\icon.ico') -Destination (Join-Path $repoRoot 'desktop\dist-brand\icon.ico')
# 盘符测试使用文本夹具；真实 PE 图标另由 verify_brand_icon 验收，这里只记录校验接线。
@'
param([string]$Executable, [string]$Icon)
$global:observedBrandIconPaths.Add($Executable)
'@ | Set-Content -LiteralPath (Join-Path $tempInstaller 'verify_brand_icon.ps1') -Encoding utf8
$fakeCompiler = Join-Path ([IO.Path]::GetTempPath()) "nearmate-iscc-$([guid]::NewGuid()).ps1"
@'
$global:observedCompilerArguments = $args
$outputArgument = @($args | Where-Object { $_ -like '/O*' })[0]
$profileArgument = @($args | Where-Object { $_ -like '/DProfile=*' })[0]
if ($global:fakeCompilerExitCode -eq 0) {
    $outputDirectory = $outputArgument.Substring(2)
    $profile = $profileArgument.Substring('/DProfile='.Length)
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    'fake setup' | Set-Content -LiteralPath (Join-Path $outputDirectory "NearMate-$profile-setup.exe")
    'fake volume' | Set-Content -LiteralPath (Join-Path $outputDirectory "NearMate-$profile-setup-1.bin")
}
$global:LASTEXITCODE = $global:fakeCompilerExitCode
'@ | Set-Content -LiteralPath $fakeCompiler -Encoding utf8

# 用假的 subst 和编译器验证盘符选择；测试绝不更改系统映射或压缩真实安装包。
function subst {
    $global:LASTEXITCODE = 0
    if ($args.Count -eq 0) { return $global:fakeMappingLines }
    $global:mappingCalls.Add(($args -join '|'))
}

try {
    $global:observedBrandIconPaths = [Collections.Generic.List[string]]::new()
    $global:fakeCompilerExitCode = 0
    $global:fakeMappingLines = @("N:\: => $repoRoot")
    $global:mappingCalls = [Collections.Generic.List[string]]::new()
    & "$repoRoot\installer\compile_setup.ps1" -Profile cuda -Iscc $fakeCompiler -DriveLetter N
    if ($global:observedBrandIconPaths.Count -ne 2 -or
        $global:observedBrandIconPaths[0] -ne (Join-Path $tempPackage 'NearMate.exe') -or
        $global:observedBrandIconPaths[1] -ne (Join-Path $repoRoot 'installer\Output\cuda\NearMate-cuda-setup.exe')) {
        throw '安装编译前后没有校验主程序与安装器的真实图标资源'
    }
    if ($global:mappingCalls.Count -ne 0) { throw '复用映射时不应创建或删除盘符' }
    if (-not ($global:observedCompilerArguments -like '/DPackageRoot=N:\*')) { throw '未复用同仓库映射' }
    $ownershipManifest = Join-Path $tempPackage 'release-root-files.txt'
    if (-not (Test-Path -LiteralPath $ownershipManifest -PathType Leaf)) { throw '未生成发行根级所有权清单' }
    $ownershipEntries = @(Get-Content -LiteralPath $ownershipManifest)
    if ('NearMate.exe' -notin $ownershipEntries -or 'release-root-files.txt' -notin $ownershipEntries) {
        throw '发行根级所有权清单缺少主程序或清单自身'
    }

    # 指定新交付目录时只清理其当前 profile；默认 Output 的既有产物必须原样保留。
    $alternateOutputRoot = Join-Path $repoRoot 'isolated-installers'
    $defaultInstallerPath = Join-Path $repoRoot 'installer\Output\cuda\NearMate-cuda-setup.exe'
    'keep old release' | Set-Content -LiteralPath $defaultInstallerPath
    & "$repoRoot\installer\compile_setup.ps1" -Profile cuda -Iscc $fakeCompiler -DriveLetter N -OutputRoot $alternateOutputRoot
    if ((Get-Content -LiteralPath $defaultInstallerPath -Raw).Trim() -ne 'keep old release' -or
        -not (Test-Path -LiteralPath (Join-Path $alternateOutputRoot 'cuda\NearMate-cuda-setup.exe'))) {
        throw '指定独立 OutputRoot 时修改了既有安装产物或没有写入新目录'
    }

    $packageJunctionTarget = Join-Path $repoRoot 'outside-package'
    $packageJunction = Join-Path $tempPackage 'stale-link'
    New-Item -ItemType Directory -Path $packageJunctionTarget -Force | Out-Null
    New-Item -ItemType Junction -Path $packageJunction -Target $packageJunctionTarget | Out-Null
    $packageJunctionRejected = $false
    try { & "$repoRoot\installer\compile_setup.ps1" -Profile cuda -Iscc $fakeCompiler -DriveLetter G }
    catch { $packageJunctionRejected = $_.Exception.Message -match 'Electron unpacked 根目录包含重解析点' }
    Remove-Item -LiteralPath $packageJunction -Force
    if (-not $packageJunctionRejected) { throw 'Electron unpacked 根目录 junction 未被拒绝' }

    'stale volume' | Set-Content -LiteralPath (Join-Path $repoRoot 'installer\Output\cuda\NearMate-cuda-setup-99.bin')
    $global:fakeMappingLines = @('N:\: => C:\another-project')
    & "$repoRoot\installer\compile_setup.ps1" -Profile cuda -Iscc $fakeCompiler -DriveLetter N
    if ($global:observedCompilerArguments -like '/DPackageRoot=N:\*') { throw '不应借用其他仓库映射' }
    if ($global:mappingCalls.Count -ne 2 -or $global:mappingCalls[1] -notlike '*|/D') { throw '临时映射没有成对释放' }
    if (Test-Path -LiteralPath (Join-Path $repoRoot 'installer\Output\cuda\NearMate-cuda-setup-99.bin')) { throw '构建前未清理旧分卷' }

    $outputJunctionTarget = Join-Path $repoRoot 'outside-output'
    New-Item -ItemType Directory -Path $outputJunctionTarget -Force | Out-Null
    Remove-Item -LiteralPath (Join-Path $repoRoot 'installer\Output') -Recurse -Force
    New-Item -ItemType Junction -Path (Join-Path $repoRoot 'installer\Output') -Target $outputJunctionTarget | Out-Null
    $junctionRejected = $false
    try { & "$repoRoot\installer\compile_setup.ps1" -Profile cuda -Iscc $fakeCompiler -DriveLetter G }
    catch { $junctionRejected = $_.Exception.Message -match '重解析点' }
    if (-not $junctionRejected) { throw 'Output 根目录 junction 未被拒绝' }
    Remove-Item -LiteralPath (Join-Path $repoRoot 'installer\Output') -Force

    $global:mappingCalls.Clear()
    $global:fakeCompilerExitCode = 17
    $failed = $false
    try { & "$repoRoot\installer\compile_setup.ps1" -Profile cuda -Iscc $fakeCompiler -DriveLetter G }
    catch { $failed = $_.Exception.Message -like '*17*' }
    if (-not $failed -or $global:mappingCalls.Count -ne 2) { throw '编译失败时未报告错误或未释放映射' }
    Write-Host '盘符回归通过：同路径复用、冲突改选、编译失败清理。'
}
finally {
    Remove-Item -LiteralPath $fakeCompiler -Force
    if (Test-Path -LiteralPath $repoRoot) {
        Remove-Item -LiteralPath $repoRoot -Recurse -Force
    }
}
