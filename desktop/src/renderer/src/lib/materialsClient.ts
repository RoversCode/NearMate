import {
  readMaterialContextSnapshot,
  type MaterialContextSnapshot
} from "../../../shared/materialContext";

/**
 * 首次加载或断线重连时读取 Backend 当前材料快照。
 *
 * @param httpBaseUrl 控制服务 HTTP 根地址。
 * @returns 经过统一协议校验的 revisioned 快照。
 */
export async function fetchCurrentMaterialContext(httpBaseUrl: string): Promise<MaterialContextSnapshot> {
  const normalizedHttpBaseUrl = httpBaseUrl.replace(/\/+$/, "");
  const response = await fetch(`${normalizedHttpBaseUrl}/api/materials/contexts/current`);
  if (!response.ok) {
    throw new Error(`material context request failed: ${response.status}`);
  }
  return readMaterialContextSnapshot(await response.json());
}
