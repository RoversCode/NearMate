<#
.SYNOPSIS
核对 Windows EXE 的实际图标资源，避免构建配置正确但安装包仍含 Electron 默认图标。

.PARAMETER Executable
待验收的 NearMate 主程序或安装程序绝对路径。

.PARAMETER Icon
本次从 assets/icon.png 生成的多尺寸 ICO 路径。

.OUTPUTS
无；至少一组 EXE 图标与品牌 ICO 全部图层完全一致时成功，否则终止构建。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [string]$Icon = (Join-Path $PSScriptRoot '..\desktop\dist-brand\icon.ico')
)

$ErrorActionPreference = 'Stop'
if (-not ('NearMate.Build.IconResourceVerifier' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;

namespace NearMate.Build {
    // 仅把 EXE 当数据读取，不运行用户程序，也不向磁盘改写 PE 资源。
    public static class IconResourceVerifier {
        private delegate bool ResourceNameCallback(IntPtr module, IntPtr type, IntPtr name, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr LoadLibraryExW(string file, IntPtr reserved, uint flags);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool FreeLibrary(IntPtr module);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool EnumResourceNamesW(IntPtr module, IntPtr type, ResourceNameCallback callback, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr FindResourceW(IntPtr module, IntPtr name, IntPtr type);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr LoadResource(IntPtr module, IntPtr resource);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint SizeofResource(IntPtr module, IntPtr resource);
        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr LockResource(IntPtr resource);

        private static byte[] ReadResource(IntPtr module, IntPtr name, int type) {
            IntPtr resource = FindResourceW(module, name, new IntPtr(type));
            uint size = SizeofResource(module, resource);
            IntPtr data = LockResource(LoadResource(module, resource));
            if (resource == IntPtr.Zero || data == IntPtr.Zero || size == 0) throw new InvalidDataException("EXE 图标资源缺失或为空");
            byte[] bytes = new byte[checked((int)size)];
            Marshal.Copy(data, bytes, 0, bytes.Length);
            return bytes;
        }

        /// <summary>逐层比较 PE 中的图标与品牌 ICO；参数为 EXE、ICO 路径，返回是否完全匹配。</summary>
        public static bool ContainsExactIcon(string executable, string icon) {
            byte[] expected = File.ReadAllBytes(icon);
            if (expected.Length < 6 || BitConverter.ToUInt16(expected, 0) != 0 || BitConverter.ToUInt16(expected, 2) != 1) throw new InvalidDataException("品牌 ICO 文件头无效");
            int count = BitConverter.ToUInt16(expected, 4);
            if (count == 0 || expected.Length < 6 + count * 16) throw new InvalidDataException("品牌 ICO 图层目录无效");
            byte[][] frames = new byte[count][];
            for (int index = 0; index < count; index++) {
                int entry = 6 + index * 16;
                int size = checked((int)BitConverter.ToUInt32(expected, entry + 8));
                int offset = checked((int)BitConverter.ToUInt32(expected, entry + 12));
                if (size <= 0 || offset < 6 + count * 16 || size > expected.Length - offset) throw new InvalidDataException("品牌 ICO 图层越界");
                frames[index] = expected.Skip(offset).Take(size).ToArray();
            }
            IntPtr module = LoadLibraryExW(executable, IntPtr.Zero, 0x22);
            if (module == IntPtr.Zero) throw new IOException("无法读取 EXE 资源", new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()));
            bool matched = false;
            try {
                EnumResourceNamesW(module, new IntPtr(14), (handle, type, name, parameter) => {
                    byte[] group = ReadResource(handle, name, 14);
                    if (group.Length < 6 || BitConverter.ToUInt16(group, 4) != count || group.Length < 6 + count * 14) return true;
                    for (int index = 0; index < count; index++) {
                        int expectedEntry = 6 + index * 16;
                        int groupEntry = 6 + index * 14;
                        // rcedit 会重新填写色深等目录字段；验收尺寸和实际图层字节才反映品牌是否一致。
                        if (!expected.Skip(expectedEntry).Take(2).SequenceEqual(group.Skip(groupEntry).Take(2))) return true;
                        ushort resourceId = BitConverter.ToUInt16(group, groupEntry + 12);
                        if (!frames[index].SequenceEqual(ReadResource(handle, new IntPtr(resourceId), 3))) return true;
                    }
                    matched = true;
                    return false;
                }, IntPtr.Zero);
                return matched;
            } finally {
                FreeLibrary(module);
            }
        }
    }
}
'@
}

$executablePath = (Resolve-Path -LiteralPath $Executable).Path
$iconPath = (Resolve-Path -LiteralPath $Icon).Path
if (-not [NearMate.Build.IconResourceVerifier]::ContainsExactIcon($executablePath, $iconPath)) {
    throw "EXE 未写入本次品牌图标的完整图层：$executablePath"
}
Write-Host "EXE 品牌图标资源验证通过：$executablePath"
