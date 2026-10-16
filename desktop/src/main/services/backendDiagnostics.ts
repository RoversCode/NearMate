import { appendFileSync, mkdirSync, renameSync, statSync } from "node:fs";
import { dirname } from "node:path";

const MAX_BACKEND_LOG_BYTES = 2 * 1024 * 1024;

/**
 * 保存后台的最新诊断信息，写满后保留一份旧日志，避免旧输出挤掉本次故障。
 *
 * Args:
 *   path: 用户数据目录中的诊断日志路径。
 *   content: 后台输出或不含凭据的生命周期记录。
 * Returns:
 *   无；磁盘不可写时跳过日志，不影响进程管理和错误提示。
 */
export function appendBackendDiagnostic(path: string, content: string | Uint8Array): void {
  try {
    let bytes = Buffer.from(content);
    if (bytes.length === 0) return;
    // 容量按实际字节计算；单次超大输出也只保留尾部，并避开 UTF-8 字符中间的截断位置。
    if (bytes.length > MAX_BACKEND_LOG_BYTES) {
      let offset = bytes.length - MAX_BACKEND_LOG_BYTES;
      while (offset < bytes.length && (bytes[offset]! & 0xc0) === 0x80) offset += 1;
      bytes = bytes.subarray(offset);
    }
    mkdirSync(dirname(path), { recursive: true });
    const currentSize = statSync(path, { throwIfNoEntry: false })?.size ?? 0;
    if (currentSize > 0 && currentSize + bytes.length > MAX_BACKEND_LOG_BYTES) {
      renameSync(path, `${path}.1`);
    }
    appendFileSync(path, bytes);
  } catch {
    // 进程退出时仍要通知用户，不能因为日志目录权限或磁盘故障阻断通知。
  }
}
