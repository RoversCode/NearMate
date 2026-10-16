export type {
  MaterialAttachment,
  MaterialContextSnapshot,
  MaterialKind,
  RecentMaterial,
  RecentMaterialStatus
} from "../../../shared/materialContext";

import type { MaterialAttachment, MaterialKind } from "../../../shared/materialContext";

export type MaterialShelfItemView = {
  materialId: string;
  kind: MaterialKind;
  mediaClass: MaterialAttachment["media_class"];
  displayName: string;
  path: string | null;
  thumbnailDataUrl?: string | null;
};

export type MaterialShelfView = {
  isVisible: boolean;
  title: string;
  countText: string;
  items: MaterialShelfItemView[];
  snapshotText: string | null;
};
