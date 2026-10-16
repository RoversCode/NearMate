import { useRef } from "react";

import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { CharacterCard, CharacterCardPayload } from "../../api/contracts/characterCards";
import { buildCharacterCardCoverUrl } from "./characterCardCoverUrl";
import { summarizeVoice } from "./characterCardFormat";

/**
 * 渲染角色卡预览面板。
 *
 * Args:
 *   card: 当前草稿角色卡。
 *   sharedWorldCanon: 后端共享世界观文本。
 *
 * Returns:
 *   角色摘要，以及按需展开的完整共享世界观。
 */
export function PreviewPanel({ card, sharedWorldCanon }: { card: CharacterCard; sharedWorldCanon: string }): JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>预览区</CardTitle>
        <CardDescription>快速查看角色称呼、人物画像、核心矛盾和共享世界观。</CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="divide-y">
          <PreviewRow label="角色称呼" value={card.meta.name} />
          <PreviewRow label="卡片标题 / 人物画像" value={card.meta.title} />
          <PreviewRow label="核心矛盾感" value={card.inner_core.core_contradiction || "未设置"} />
          <PreviewRow label="声纹摘要" value={summarizeVoice(card.voiceprint.speech_style)} />
        </dl>
        <details className="mt-4 border-t pt-3">
          <summary className="cursor-pointer rounded-sm text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2">
            共享世界观
          </summary>
          <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-relaxed text-muted-foreground">
            {sharedWorldCanon || "尚未配置共享世界观。"}
          </p>
        </details>
      </CardContent>
    </Card>
  );
}

/**
 * 渲染预览面板中的单条键值行。
 *
 * Args:
 *   label: 字段名称。
 *   value: 字段内容。
 *
 * Returns:
 *   标签在上、内容在下的定义行。
 */
function PreviewRow({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="space-y-1 py-3 first:pt-0 last:pb-0">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="whitespace-pre-wrap text-sm leading-relaxed">{value}</dd>
    </div>
  );
}

/**
 * 渲染角色卡形象照片面板。
 *
 * Args:
 *   card: 当前草稿角色卡。
 *   disabled: 是否禁用上传和删除。
 *   onUploadCover: 选择源图片后的回调。
 *   onDeleteCover: 删除封面回调。
 *
 * Returns:
 *   封面预览，以及鼠标和键盘均可操作的上传、删除按钮。
 */
export function CoverPanel({
  card,
  disabled,
  onUploadCover,
  onDeleteCover
}: {
  card: CharacterCard;
  disabled: boolean;
  onUploadCover?: (cardId: string, imageFile: File) => Promise<void>;
  onDeleteCover?: (cardId: string) => Promise<void>;
}): JSX.Element {
  const coverUrl = buildCharacterCardCoverUrl(card);
  const coverInputRef = useRef<HTMLInputElement | null>(null);

  function handleCoverChanged(event: React.ChangeEvent<HTMLInputElement>): void {
    const selectedFile = event.target.files?.[0] ?? null;
    event.target.value = "";
    if (selectedFile === null || onUploadCover === undefined) {
      return;
    }
    void onUploadCover(card.meta.card_id, selectedFile);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>形象照片</CardTitle>
        <CardDescription>选择 PNG/JPEG 后先裁剪构图，成品会压缩为 2MB 内的 JPEG 封面。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid aspect-[4/4.6] place-items-center overflow-hidden rounded-lg bg-[linear-gradient(145deg,rgba(11,142,168,0.20),rgba(132,94,194,0.20)_48%,rgba(255,184,108,0.22))] dark:bg-[linear-gradient(145deg,rgba(11,142,168,0.28),rgba(132,94,194,0.26)_48%,rgba(255,184,108,0.24))]">
          {coverUrl !== null ? (
            <img alt={`${card.meta.name || "角色"}形象照片`} className="h-full w-full object-cover" src={coverUrl} />
          ) : (
            <span
              aria-hidden="true"
              className="grid size-20 place-items-center rounded-full border border-white/70 bg-white/50 text-4xl font-black text-[rgba(8,39,53,0.72)] shadow-lg backdrop-blur-sm dark:border-white/20 dark:bg-white/10 dark:text-white/80"
            >
              {(card.meta.name.trim() || "?").slice(0, 1)}
            </span>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={disabled || onUploadCover === undefined}
            onClick={() => coverInputRef.current?.click()}
            type="button"
            variant="outline"
          >
            上传形象照片
          </Button>
          <input
            accept="image/png,image/jpeg,.png,.jpg,.jpeg"
            aria-label="上传形象照片"
            className="hidden"
            disabled={disabled || onUploadCover === undefined}
            onChange={handleCoverChanged}
            ref={coverInputRef}
            type="file"
          />
          <button
            className={buttonVariants({ variant: "outline" })}
            disabled={disabled || coverUrl === null || onDeleteCover === undefined}
            onClick={() => void onDeleteCover?.(card.meta.card_id)}
            type="button"
          >
            删除形象照片
          </button>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 渲染角色卡校验信息面板。
 *
 * Args:
 *   payload: 后端返回的角色卡响应。
 *
 * Returns:
 *   校验消息列表；没有问题时只显示简短结果。
 */
export function ValidationPanel({ payload }: { payload: CharacterCardPayload }): JSX.Element {
  if (payload.validation_messages.length === 0) {
    return (
      <Card>
        <CardContent className="px-5 py-4">
          <p className="text-sm text-muted-foreground">暂无校验问题。</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>校验区</CardTitle>
        <CardDescription>展示后端返回的校验信息。</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="list-disc space-y-1.5 pl-5 text-sm leading-relaxed text-warning">
          {payload.validation_messages.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
