import { useRef, useState } from "react";
import type { RecentMaterial, RecentMaterialStatus } from "./materialTypes";

export type RecentMaterialShelfProps = {
  materials: RecentMaterial[];
  refresh(): Promise<void>;
  forget(path: string): Promise<void>;
};

/**
 * 展示 NearMate 当前仍能直接定位的近期本地材料。
 *
 * Args:
 *   props: Backend 权威快照中的近期材料，以及刷新和忘记命令。
 *
 * Returns:
 *   没有近期材料时不占空间；否则返回独立于本轮材料的路径能力列表。
 */
export function RecentMaterialShelf({ materials, refresh, forget }: RecentMaterialShelfProps) {
  const [isRefreshing, setIsRefreshing] = useState(false);
  const isRefreshingRef = useRef(false);
  const [forgettingPaths, setForgettingPaths] = useState<Set<string>>(() => new Set());
  const forgettingPathsRef = useRef<Set<string>>(new Set());

  if (materials.length === 0) {
    return null;
  }

  return (
    <section className="recent-material-shelf" aria-label="NearMate 当前记得">
      <div className="recent-material-shelf-header">
        <strong>NearMate 当前记得</strong>
        <button
          type="button"
          disabled={isRefreshing}
          onClick={async () => {
            if (isRefreshingRef.current) {
              return;
            }
            isRefreshingRef.current = true;
            setIsRefreshing(true);
            try {
              await refresh();
            } catch {
              // runtime 统一展示错误；列表只恢复按钮，让用户可以再次刷新。
            } finally {
              isRefreshingRef.current = false;
              setIsRefreshing(false);
            }
          }}
        >
          {isRefreshing ? "刷新中…" : "刷新"}
        </button>
      </div>
      <ul className="recent-material-list">
        {materials.map(material => (
          <li key={material.path} className="recent-material-item" data-status={material.status}>
            <div className="recent-material-main">
              <span className="recent-material-name" title={material.display_name}>
                {material.display_name}
              </span>
              <span className="recent-material-meta">
                {material.node_kind === "directory" ? "目录" : "文件"}
                <span aria-hidden="true"> · </span>
                {recentMaterialStatusLabel(material.status)}
              </span>
            </div>
            <button
              type="button"
              className="recent-material-forget"
              aria-label={`忘记 ${material.display_name}`}
              disabled={forgettingPaths.has(material.path)}
              onClick={async () => {
                if (forgettingPathsRef.current.has(material.path)) {
                  return;
                }
                forgettingPathsRef.current.add(material.path);
                setForgettingPaths(current => new Set(current).add(material.path));
                try {
                  await forget(material.path);
                } catch {
                  // runtime 保留原快照并展示统一错误；这里只恢复可重试状态。
                } finally {
                  forgettingPathsRef.current.delete(material.path);
                  setForgettingPaths(current => {
                    const next = new Set(current);
                    next.delete(material.path);
                    return next;
                  });
                }
              }}
            >
              忘记
            </button>
            <details className="recent-material-path">
              <summary>查看路径</summary>
              <code>{material.path}</code>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * 把 Backend 稳定状态转换成用户能理解的短文案。
 *
 * Args:
 *   status: Backend 对路径根节点做轻量检查后的状态。
 *
 * Returns:
 *   近期材料列表使用的中文状态名称。
 */
function recentMaterialStatusLabel(status: RecentMaterialStatus): string {
  const labels: Record<RecentMaterialStatus, string> = {
    available: "可用",
    missing: "路径不存在",
    type_changed: "类型已变化",
    unsafe: "路径不安全",
    unreadable: "无法读取"
  };
  return labels[status];
}
