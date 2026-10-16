import type { ActivitySnapshot } from "../types/backendEvents";

/**
 * 按 runtime/revision 规则裁决是否接受一份新 Activity 快照。
 *
 * 规则：当前无快照时接受；runtime_id 不同说明 Backend 已重启，无条件替换
 * （即使 revision 更小）；同一 runtime 只接受更高 revision。
 *
 * Args:
 *   current: 当前持有的快照；从未收到时为空。
 *   next: 已通过契约校验的新快照。
 *
 * Returns:
 *   新快照应当替换当前快照时返回真。
 */
export function shouldAcceptActivitySnapshot(
  current: ActivitySnapshot | null,
  next: ActivitySnapshot
): boolean {
  if (current === null) {
    return true;
  }
  if (next.runtime_id !== current.runtime_id) {
    return true;
  }
  return next.revision > current.revision;
}
