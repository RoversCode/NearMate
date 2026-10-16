import { useEffect, useMemo, useRef, useState } from "react";
import { buildMaterialShelfView } from "./materialSummary";
import type { MaterialKind, MaterialContextSnapshot, MaterialShelfItemView } from "./materialTypes";

export type MaterialShelfProps = {
  pool: MaterialContextSnapshot | null;
  removeMaterialItem(materialId: string): Promise<void>;
  getMaterialThumbnail?(path: string): Promise<string | null>;
  onMaterialRemoved?(): void;
};

/**
 * MaterialShelf 渲染当前待提交 turn 的材料栏。
 *
 * @param props.pool 后端当前材料上下文快照。
 * @returns 有材料时返回资料栏；无材料时不占界面空间。
 */
export function MaterialShelf({ pool, removeMaterialItem, getMaterialThumbnail, onMaterialRemoved }: MaterialShelfProps) {
  const [thumbnailCache, setThumbnailCache] = useState<Record<string, string | null>>({});
  const [deletingMaterialIds, setDeletingMaterialIds] = useState<Set<string>>(() => new Set());
  const deletingMaterialIdsRef = useRef<Set<string>>(new Set());
  const poolWithThumbnails = useMemo(() => {
    if (!pool) {
      return null;
    }

    const stagedMaterials = pool.turn_material_context.staged_materials ?? [];
    return {
      ...pool,
      turn_material_context: {
        ...pool.turn_material_context,
        staged_materials: stagedMaterials.map(item => {
          const cacheKey = buildThumbnailCacheKey(item.material_id, item.path);
          const existingThumbnailDataUrl = (item as { thumbnailDataUrl?: string | null }).thumbnailDataUrl ?? null;
          return {
            ...item,
            thumbnailDataUrl: thumbnailCache[cacheKey] ?? existingThumbnailDataUrl
          };
        })
      }
    };
  }, [pool, thumbnailCache]);
  const view = buildMaterialShelfView(poolWithThumbnails);

  useEffect(() => {
    if (!getMaterialThumbnail || !pool) {
      return;
    }

    let disposed = false;
    for (const item of pool.turn_material_context.staged_materials ?? []) {
      if (!item.path) {
        continue;
      }

      const cacheKey = buildThumbnailCacheKey(item.material_id, item.path);
      if (cacheKey in thumbnailCache) {
        continue;
      }

      void getMaterialThumbnail(item.path).then(thumbnailDataUrl => {
        if (disposed) {
          return;
        }

        setThumbnailCache(current => ({
          ...current,
          [cacheKey]: thumbnailDataUrl
        }));
      }).catch(() => {
        if (disposed) {
          return;
        }

        setThumbnailCache(current => ({
          ...current,
          [cacheKey]: null
        }));
      });
    }

    return () => {
      disposed = true;
    };
  }, [getMaterialThumbnail, pool, thumbnailCache]);

  if (!view.isVisible) {
    return null;
  }

  return (
    <section className="material-shelf" aria-label="本轮材料">
      <div className="material-shelf-header">
        <strong>{view.title}</strong>
        <span>{view.countText}</span>
      </div>
      <ul className="material-shelf-strip">
        {view.items.map(item => (
          <li className="material-shelf-thumb" key={item.materialId}>
            <MaterialThumbnail item={item} />
            <span title={materialItemTitle(item)}>
              {item.displayName}
            </span>
            <button
              type="button"
              aria-label={`删除 ${item.displayName}`}
              disabled={deletingMaterialIds.has(item.materialId)}
              onClick={async () => {
                if (deletingMaterialIdsRef.current.has(item.materialId)) {
                  return;
                }
                deletingMaterialIdsRef.current.add(item.materialId);
                setDeletingMaterialIds(current => new Set(current).add(item.materialId));
                try {
                  await removeMaterialItem(item.materialId);
                  onMaterialRemoved?.();
                } catch {
                  // runtime 负责记录诊断和展示统一错误，材料栏只恢复可重试状态。
                } finally {
                  deletingMaterialIdsRef.current.delete(item.materialId);
                  setDeletingMaterialIds(current => {
                    const next = new Set(current);
                    next.delete(item.materialId);
                    return next;
                  });
                }
              }}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function MaterialThumbnail({ item }: { item: MaterialShelfItemView }) {
  if (item.thumbnailDataUrl) {
    return <img src={item.thumbnailDataUrl} alt="" draggable={false} />;
  }

  return <div className="material-shelf-fallback" aria-hidden="true">{fallbackLabel(item)}</div>;
}

function fallbackLabel(item: MaterialShelfItemView): string {
  if (item.mediaClass === "audio") {
    return "音";
  }

  if (item.mediaClass === "video") {
    return "视";
  }

  const kind: MaterialKind = item.kind;
  if (kind === "dragged_folder") {
    return "夹";
  }

  if (kind === "clipboard_text" || kind === "selected_text") {
    return "文";
  }

  if (kind === "selected_web") {
    return "网";
  }

  if (kind === "clipboard_image" || kind === "selected_image") {
    return "图";
  }

  return "件";
}

function buildThumbnailCacheKey(materialId: string, path: string | null): string {
  return `${materialId}:${path ?? ""}`;
}

function materialItemTitle(item: MaterialShelfItemView): string {
  return item.displayName;
}
