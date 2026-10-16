/**
 * 为跨平台开发命令生成不会触发 Windows EINVAL 的子进程启动参数。
 *
 * Args:
 *   executable: 要执行的文件名或路径。
 *   args: 传给命令的参数。
 *
 * Returns:
 *   可直接传给 child_process.spawn 的可执行文件、参数和 shell 选项。
 *
 * 边界说明：Windows 的 cmd/bat 不是原生可执行文件，不能直接用 shell=false 传给 Node；这里只把它们交给明确的 ComSpec，Node 层仍不打开 shell。
 */
export function buildSpawnSpec(executable, args) {
  const isWindowsCommandShim = process.platform === "win32" && /\.(?:cmd|bat)$/i.test(executable);
  if (!isWindowsCommandShim) {
    return { executable, args, shell: false };
  }

  const commandLine = [executable, ...args].map(quoteWindowsCommandArgument).join(" ");
  return {
    executable: process.env.ComSpec?.trim() || "cmd.exe",
    args: ["/d", "/s", "/c", commandLine],
    shell: false,
  };
}

function quoteWindowsCommandArgument(value) {
  if (/^[A-Za-z0-9_./\\:=+-]+$/.test(value)) {
    return value;
  }
  return `"${value.replaceAll("%", "%%").replaceAll("\"", "\\\"")}"`;
}
