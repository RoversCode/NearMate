function Assert-NoReparsePointInPath([string]$Path, [string]$Description) {
    <#
    .SYNOPSIS
    从目标向上检查所有已经存在的路径段，拒绝任何重解析点。

    .PARAMETER Path
    待检查的完整路径；不存在的尾部允许稍后创建。

    .PARAMETER Description
    错误消息中使用的路径说明。
    #>
    $currentPath = [IO.Path]::GetFullPath($Path)
    while ($true) {
        $existingItem = Get-Item -LiteralPath $currentPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $existingItem -and
            ($existingItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Description 不能经过符号链接、目录联接或其它重解析点：$currentPath"
        }
        $parentPath = Split-Path -Path $currentPath -Parent
        if ([string]::IsNullOrWhiteSpace($parentPath) -or $parentPath -eq $currentPath) {
            return
        }
        $currentPath = $parentPath
    }
}

function Assert-NoReparsePoints([string]$Path, [string]$Description) {
    <#
    .SYNOPSIS
    扫描即将被递归复制或删除的目录，拒绝其中任何重解析点。

    .PARAMETER Path
    待扫描的目录或文件路径；不存在的路径直接返回。

    .PARAMETER Description
    错误消息中使用的路径说明。
    #>
    Assert-NoReparsePointInPath $Path $Description
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $items = @(
        Get-Item -LiteralPath $Path -Force
        Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    )
    foreach ($item in $items) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Description 包含符号链接、目录联接或其它重解析点：$($item.FullName)"
        }
    }
}
