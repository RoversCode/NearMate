[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("cuda", "universal")]
    [string]$Profile,
    [string]$Iscc = "C:\Users\$env:USERNAME\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
    [string]$OutputRoot = "$PSScriptRoot\Output",
    [ValidatePattern("^[A-Za-z]$")]
    [char]$DriveLetter = "N"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "build_safety.ps1")
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packageRoot = Join-Path $repoRoot "desktop\dist-package\$Profile-unpacked\win-unpacked"
$issPath = Join-Path $PSScriptRoot "NearMate.iss"
$outputRoot = [IO.Path]::GetFullPath($OutputRoot).TrimEnd('\')
$outputDir = [IO.Path]::GetFullPath((Join-Path $outputRoot $Profile))

function Reset-ProfileOutputDirectory([string]$Path, [string]$Root) {
    <#
    .SYNOPSIS
    只清空当前 profile 的安装输出目录，并重新创建空目录。

    .PARAMETER Path
    当前 profile 的精确输出目录。

    .PARAMETER Root
    允许清理的安装输出根目录。
    #>
    if (-not $Path.StartsWith("$Root\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "安装输出目录超出允许范围：$Path"
    }
    Assert-NoReparsePointInPath $Root "安装输出根目录"
    Assert-NoReparsePointInPath $Path "安装输出目录"
    if (Test-Path -LiteralPath $Path) {
        $existingItem = Get-Item -LiteralPath $Path -Force
        if (($existingItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "安装输出目录不能是符号链接、目录联接或其它重解析点：$Path"
        }
        if (-not $existingItem.PSIsContainer) {
            throw "安装输出目录不是目录：$Path"
        }
        Assert-NoReparsePoints $Path "安装输出目录"
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Get-CurrentSetupArtifacts([string]$Path, [string]$CurrentProfile) {
    <#
    .SYNOPSIS
    验证本次 Inno Setup 生成的安装程序和全部分卷，并返回它们。

    .PARAMETER Path
    当前 profile 的安装输出目录。

    .PARAMETER CurrentProfile
    输出文件名中的 profile 名称。

    .OUTPUTS
    安装程序和按编号排序的 .bin 分卷文件对象。
    #>
    $installerName = "NearMate-$CurrentProfile-setup.exe"
    $installerPath = Join-Path $Path $installerName
    if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
        throw "本次构建未生成安装程序：$installerPath"
    }
    $installer = Get-Item -LiteralPath $installerPath -Force
    if ($installer.Length -le 0) {
        throw "本次构建生成的安装程序为空：$installerPath"
    }

    $binPattern = "^NearMate-$([regex]::Escape($CurrentProfile))-setup-(\d+)\.bin$"
    $packagedFiles = @(Get-ChildItem -LiteralPath $Path -File | Where-Object { $_.Extension -in @('.exe', '.bin') })
    $binFiles = @($packagedFiles | Where-Object { $_.Name -match $binPattern } | Sort-Object {
        [int]([regex]::Match($_.Name, $binPattern).Groups[1].Value)
    })
    if ($binFiles.Count -eq 0) {
        throw "本次构建未生成 $CurrentProfile 安装程序的 .bin 分卷：$Path"
    }
    $expectedNumbers = @(1..$binFiles.Count)
    $actualNumbers = @($binFiles | ForEach-Object { [int]([regex]::Match($_.Name, $binPattern).Groups[1].Value) })
    if (($actualNumbers -join ',') -ne ($expectedNumbers -join ',')) {
        throw "安装程序分卷编号不连续：$($actualNumbers -join ',')"
    }
    foreach ($file in $binFiles) {
        if ($file.Length -le 0) {
            throw "本次构建生成的安装程序分卷为空：$($file.FullName)"
        }
    }

    $unexpectedFiles = @($packagedFiles | Where-Object {
        $_.Name -ne $installerName -and $_.Name -notmatch $binPattern
    })
    if ($unexpectedFiles.Count -gt 0) {
        throw "安装输出包含非本次 profile 的 exe/bin：$($unexpectedFiles.Name -join ', ')"
    }
    return @($installer) + $binFiles
}

function Write-ReleaseOwnershipManifest([string]$Path) {
    <#
    .SYNOPSIS
    记录本次 Electron unpacked 目录的根级所有权，供升级时精确清理旧发行文件。

    .PARAMETER Path
    本次 Inno Setup 的唯一输入目录。
    #>
    Assert-NoReparsePointInPath $Path "Electron unpacked 根目录"
    $rootItems = @(Get-ChildItem -LiteralPath $Path -Force)
    foreach ($item in $rootItems) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Electron unpacked 根目录包含重解析点，不能登记发行所有权：$($item.FullName)"
        }
    }
    $manifestPath = Join-Path $Path 'release-root-files.txt'
    $entries = @(
        $rootItems |
            Where-Object { $_.Name -ne 'release-root-files.txt' } |
            ForEach-Object { [string]$_.Name }
    )
    if ($entries.Count -eq 0) {
        throw "Electron unpacked 目录没有可登记的发行文件：$Path"
    }

    # 清单自身也由发行包拥有，升级时必须一并删除，不能留在旧目录冒充新版本。
    $entries += 'release-root-files.txt'
    $entries = @($entries | Sort-Object -Unique)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($manifestPath, $entries, $utf8NoBom)
}

if (-not (Test-Path -LiteralPath $Iscc)) {
    throw "找不到 Inno Setup 编译器：$Iscc"
}
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "NearMate.exe"))) {
    throw "缺少 $Profile electron-builder unpacked 目录：$packageRoot"
}

# 检查真实 PE 资源；只检查 builder 配置会漏掉默认图标被原样带入安装包的问题。
$shortcutIcon = Join-Path $packageRoot 'resources\icon.ico'
if (-not (Test-Path -LiteralPath $shortcutIcon -PathType Leaf)) {
    throw "安装包缺少独立快捷方式品牌图标：$shortcutIcon"
}
$generatedIcon = Join-Path $repoRoot 'desktop\dist-brand\icon.ico'
if ((Get-FileHash -LiteralPath $shortcutIcon -Algorithm SHA256).Hash -ne
    (Get-FileHash -LiteralPath $generatedIcon -Algorithm SHA256).Hash) {
    throw '安装包快捷方式图标与本次生成的品牌图标不一致'
}
& (Join-Path $PSScriptRoot 'verify_brand_icon.ps1') -Executable (Join-Path $packageRoot 'NearMate.exe') -Icon $shortcutIcon

# Inno Setup 不再维护手写 Electron 文件名单；所有根级发行文件都由本次 unpacked 产物生成唯一清单。
Write-ReleaseOwnershipManifest $packageRoot

# 旧分卷会让交付目录看起来像本次构建的结果；先清空精确 profile 目录，避免混入历史产物。
Reset-ProfileOutputDirectory $outputDir $outputRoot

# subst 只能不带参数枚举映射；查询单个盘符会返回本地化错误，不能拿错误文字判断占用。
$mappings = @{}
$mappingLines = & subst
if ($LASTEXITCODE -ne 0) { throw "无法读取现有盘符映射" }
foreach ($line in $mappingLines) {
    if ($line -match '^([A-Za-z]):\\:\s*=>\s*(.+)$') {
        $mappings[$Matches[1].ToUpperInvariant()] = [IO.Path]::GetFullPath($Matches[2].Trim()).TrimEnd('\')
    }
}
$occupiedDrives = @([IO.Directory]::GetLogicalDrives() | ForEach-Object { $_.Substring(0, 1).ToUpperInvariant() })
$candidates = @(([string]$DriveLetter).ToUpperInvariant()) + @([char[]](90..68) | ForEach-Object { [string]$_ })
$selectedDrive = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if ($mappings[$candidate] -eq $repoRoot.TrimEnd('\') -or
        (-not $mappings.ContainsKey($candidate) -and $candidate -notin $occupiedDrives)) {
        $selectedDrive = $candidate
        break
    }
}
if (-not $selectedDrive) { throw "没有可用于缩短构建路径的空闲盘符" }
$driveName = "$selectedDrive`:\"
$driveSpecifier = "$selectedDrive`:"
$mappedHere = $false
try {
    if (-not $mappings.ContainsKey($selectedDrive)) {
        & subst $driveSpecifier $repoRoot
        if ($LASTEXITCODE -ne 0) {
            throw "无法创建构建路径映射：$driveName"
        }
        $mappedHere = $true
    }

    # Inno Setup 对传统 Windows 路径仍有限制；短盘符可以把深层 Python 包路径压回 260 字符以内。
    $shortPackageRoot = [IO.Path]::Combine($driveName, "desktop\dist-package\$Profile-unpacked\win-unpacked")
    Write-Host "安装构建使用路径映射：$driveName -> $repoRoot（本次创建：$mappedHere）"
    & $Iscc "/O$outputDir" "/DProfile=$Profile" "/DPackageRoot=$shortPackageRoot" $issPath
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup 编译失败，退出码：$LASTEXITCODE"
    }
}
finally {
    if ($mappedHere) {
        & subst $driveSpecifier /D | Out-Null
    }
}

$artifacts = Get-CurrentSetupArtifacts $outputDir $Profile
& (Join-Path $PSScriptRoot 'verify_brand_icon.ps1') -Executable (Join-Path $outputDir "NearMate-$Profile-setup.exe") -Icon $shortcutIcon
Write-Host "已生成安装程序及分卷：$($artifacts.Name -join ', ')"
