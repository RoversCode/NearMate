import { useState } from "react";
import Cropper, { type Area, type Point } from "react-easy-crop";
import "react-easy-crop/react-easy-crop.css";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from "@/components/ui/dialog";
import {
  CHARACTER_CARD_COVER_ASPECT,
  type CoverImagePreview,
  createCroppedCoverFile
} from "./coverImageProcessing";

type CoverCropperDialogProps = {
  preview: CoverImagePreview;
  disabled: boolean;
  onCancel: () => void;
  onConfirm: (file: File) => Promise<void>;
  onError: (message: string) => void;
};

/**
 * 渲染角色卡封面的裁剪确认弹窗。
 *
 * Args:
 *   preview: 已加载的原图预览对象。
 *   disabled: 外层保存状态是否禁用交互。
 *   onCancel: 关闭弹窗并丢弃当前裁剪。
 *   onConfirm: 收到裁剪压缩后的 JPEG 文件。
 *   onError: 裁剪或压缩失败时展示错误。
 *
 * Returns:
 *   固定角色卡封面比例的裁剪器和确认操作区。
 */
export function CoverCropperDialog({
  preview,
  disabled,
  onCancel,
  onConfirm,
  onError
}: CoverCropperDialogProps): JSX.Element {
  const [crop, setCrop] = useState<Point>({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [cropPixels, setCropPixels] = useState<Area | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);

  async function handleConfirm(): Promise<void> {
    if (disabled || isProcessing || cropPixels === null) {
      return;
    }

    setIsProcessing(true);
    try {
      const croppedFile = await createCroppedCoverFile({
        image: preview.image,
        cropPixels
      });
      await onConfirm(croppedFile);
    } catch (error) {
      onError(error instanceof Error ? error.message : "形象照片处理失败");
    } finally {
      setIsProcessing(false);
    }
  }

  function handleReset(): void {
    setCrop({ x: 0, y: 0 });
    setZoom(1);
  }

  return (
    <Dialog
      // 非模态与迁移前的自定义遮罩行为一致：背景内容保留在可访问树中，遮罩层仍然拦截指针操作。
      modal={false}
      onOpenChange={(open) => {
        // 弹窗以受控方式随裁剪草稿出现；Esc 或点击遮罩关闭等同于取消本次裁剪。
        if (!open) {
          onCancel();
        }
      }}
      open
    >
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>裁剪形象照片</DialogTitle>
          <DialogDescription>拖动画面调整构图，缩放到她最适合作为卡片封面的样子。</DialogDescription>
        </DialogHeader>
        <div className="relative h-96 overflow-hidden rounded-lg bg-muted">
          <Cropper
            aspect={CHARACTER_CARD_COVER_ASPECT}
            crop={crop}
            image={preview.objectUrl}
            maxZoom={4}
            minZoom={1}
            onCropChange={setCrop}
            onCropComplete={(_area, nextCropPixels) => setCropPixels(nextCropPixels)}
            onZoomChange={setZoom}
            showGrid={false}
            zoom={zoom}
          />
        </div>
        <label className="flex items-center gap-3">
          <span className="shrink-0 text-sm text-muted-foreground">缩放</span>
          <input
            aria-label="形象照片缩放"
            className="w-full accent-primary"
            disabled={disabled || isProcessing}
            max={4}
            min={1}
            onChange={(event) => setZoom(Number(event.target.value))}
            step={0.01}
            type="range"
            value={zoom}
          />
        </label>
        <DialogFooter>
          <Button disabled={disabled || isProcessing} onClick={handleReset} type="button" variant="outline">
            重置
          </Button>
          <Button disabled={disabled || isProcessing} onClick={onCancel} type="button" variant="outline">
            取消
          </Button>
          <Button
            disabled={disabled || isProcessing || cropPixels === null}
            onClick={() => void handleConfirm()}
            type="button"
          >
            {isProcessing ? "正在处理照片" : "确认使用"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
