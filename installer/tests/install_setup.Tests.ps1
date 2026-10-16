[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidateSet('cuda', 'universal')][string]$Profile)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$installer = Join-Path $repoRoot "installer\Output\$Profile\NearMate-$Profile-setup.exe"
$work = Join-Path $repoRoot ".artifacts\installation-tests\$Profile-$([guid]::NewGuid())"
$installDirectory = Join-Path $repoRoot ".artifacts\NearMate test $Profile"
$uninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8D5D75A7-8E4F-4F7D-9E1D-5A7E1A7E9D01}_is1'

# 实际安装会登记同一应用标识；已有用户安装时停止，不能覆盖它来做测试。
foreach ($hive in @('HKCU:', 'HKLM:')) {
    if (Test-Path -LiteralPath "$hive\$uninstallKey") { throw '本机已有 NearMate 安装，不能执行隔离安装测试' }
}
if (-not (Test-Path -LiteralPath $installer)) { throw "缺少安装程序：$installer" }
if (Test-Path -LiteralPath $installDirectory) { throw "测试目录已存在，不覆盖其中内容：$installDirectory" }
New-Item -ItemType Directory -Path $work -Force | Out-Null
$uninstaller = Join-Path $installDirectory 'unins000.exe'
try {
    $arguments = @('/SP-', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', '/CURRENTUSER',
        "/DIR=`"$installDirectory`"", "/LOG=`"$(Join-Path $work 'install.log')`"")
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "安装失败：$($process.ExitCode)，日志：$work" }
    $runtime = Join-Path $installDirectory 'resources\runtime'
    if (-not (Test-Path -LiteralPath (Join-Path $installDirectory 'release-root-files.txt') -PathType Leaf)) {
        throw '安装后的程序缺少根级所有权清单'
    }
    $python = Join-Path $runtime 'python\Scripts\python.exe'
    & $python -I -B -X utf8 (Join-Path $repoRoot 'installer\verify_python_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw '安装后的 Python 不独立' }
    & $python -I -B -X utf8 (Join-Path $repoRoot 'installer\verify_backend_startup.py') $runtime (Join-Path $work 'user-data')
    if ($LASTEXITCODE -ne 0) { throw '安装后的首次启动失败' }
    # 开源安装包保留实际导入的业务源码，避免发行时又回到受保护字节码入口。
    foreach ($relative in @('companion\core\main.py', 'audio_aec_sidecar\main.py', 'audio_aec_poc\main.py')) {
        if (-not (Test-Path -LiteralPath (Join-Path $runtime "python\Lib\site-packages\$relative") -PathType Leaf)) {
            throw "安装后的开源业务入口缺失：$relative"
        }
    }
    Write-Host "本机 $Profile 安装、Python 重定位、Backend 和 Console 检查通过；日志：$work"
}
finally {
    if (Test-Path -LiteralPath $uninstaller) {
        $process = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$(Join-Path $work 'uninstall.log')`"") -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "测试安装卸载失败，退出码：$($process.ExitCode)，目录：$installDirectory" }
    }
}
if (Test-Path -LiteralPath (Join-Path $installDirectory 'NearMate.exe')) { throw '测试安装卸载后仍保留主程序' }
Write-Host "已卸载本次临时安装，测试记录保留在：$work"
