import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { CharacterCard } from "../../api/contracts/characterCards";

type CharacterCardGalleryProps = {
  cards: CharacterCard[];
  activeCardId: string;
  selectedCardId: string;
  disabled: boolean;
  onOpenCard: (card: CharacterCard) => void;
};

type CharacterGalleryCardProps = {
  card: CharacterCard;
  disabled: boolean;
  isActive: boolean;
  isSelected: boolean;
  onOpenCard: (card: CharacterCard) => void;
};

/**
 * 渲染角色卡图库。
 *
 * Args:
 *   cards: 可展示和进入编辑的完整角色卡列表。
 *   activeCardId: 当前后端激活的角色卡编号。
 *   selectedCardId: 当前页面草稿对应的角色卡编号。
 *   disabled: 是否禁用卡片点击。
 *   onOpenCard: 点击卡片后进入详情编辑的回调。
 *
 * Returns:
 *   带形象照片或占位图、角色称呼和人物画像的卡片墙。
 */
export function CharacterCardGallery({
  cards,
  activeCardId,
  selectedCardId,
  disabled,
  onOpenCard
}: CharacterCardGalleryProps): JSX.Element {
  return (
    <div aria-label="角色卡图库列表" className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-5">
      {cards.map((card) => (
        <CharacterGalleryCard
          card={card}
          disabled={disabled}
          isActive={card.meta.card_id === activeCardId}
          isSelected={card.meta.card_id === selectedCardId}
          key={card.meta.card_id}
          onOpenCard={onOpenCard}
        />
      ))}
    </div>
  );
}

/**
 * 渲染图库中的单张角色卡。
 *
 * Args:
 *   card: 完整角色卡数据。
 *   disabled: 是否禁用点击。
 *   isActive: 是否为后端激活卡。
 *   isSelected: 是否为当前草稿卡。
 *   onOpenCard: 进入详情编辑的回调。
 *
 * Returns:
 *   封面 + 文案的可点击卡片，带悬停浮起与选中描边效果。
 */
function CharacterGalleryCard({
  card,
  disabled,
  isActive,
  isSelected,
  onOpenCard
}: CharacterGalleryCardProps): JSX.Element {
  const description = buildGalleryDescription(card);
  return (
    <button
      aria-label={`${card.meta.name}，${card.meta.title}${isActive ? "，当前激活" : ""}`}
      className={cn(
        "group overflow-hidden rounded-xl border bg-card text-left shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
        isSelected && "border-primary/60 ring-2 ring-primary/20"
      )}
      disabled={disabled}
      onClick={() => onOpenCard(card)}
      type="button"
    >
      <CharacterAvatar card={card} />
      <span className="block space-y-1.5 p-4">
        <span className="flex items-center justify-between gap-2">
          <strong className="truncate text-sm font-semibold">{card.meta.name || "未命名角色"}</strong>
          {isActive ? <Badge variant="success">当前激活</Badge> : null}
        </span>
        <span className="block truncate text-xs text-muted-foreground">
          {card.meta.title || "未填写卡片标题 / 人物画像"}
        </span>
        <span className="block text-xs leading-relaxed text-muted-foreground line-clamp-2">{description}</span>
      </span>
    </button>
  );
}

/**
 * 渲染角色卡封面：有形象照片用照片，否则用品牌渐变占位 + 首字母。
 *
 * Args:
 *   card: 完整角色卡数据。
 *
 * Returns:
 *   4:4.8 比例的封面区块。
 */
function CharacterAvatar({ card }: { card: CharacterCard }): JSX.Element {
  if (card.meta.avatar_path !== null && card.meta.avatar_path.trim()) {
    return (
      <span className="block aspect-[4/4.8] overflow-hidden">
        <img
          alt={`${card.meta.name || "角色"}封面`}
          className="h-full w-full object-cover transition-transform duration-300 group-hover:scale-105"
          src={buildCharacterCardCoverUrl(card)}
        />
      </span>
    );
  }

  const displayName = card.meta.name.trim() || "?";
  return (
    <span
      aria-hidden="true"
      className="grid aspect-[4/4.8] place-items-center bg-[linear-gradient(145deg,rgba(11,142,168,0.20),rgba(132,94,194,0.20)_48%,rgba(255,184,108,0.22))] dark:bg-[linear-gradient(145deg,rgba(11,142,168,0.28),rgba(132,94,194,0.26)_48%,rgba(255,184,108,0.24))]"
    >
      <span className="grid size-20 place-items-center rounded-full border border-white/70 bg-white/50 text-4xl font-black text-[rgba(8,39,53,0.72)] shadow-lg backdrop-blur-sm dark:border-white/20 dark:bg-white/10 dark:text-white/80">
        {displayName.slice(0, 1)}
      </span>
    </span>
  );
}

function buildCharacterCardCoverUrl(card: CharacterCard): string {
  return `/api/character-cards/${encodeURIComponent(card.meta.card_id)}/asset/${card.meta.avatar_path ?? ""}`;
}

function buildGalleryDescription(card: CharacterCard): string {
  const candidates = [
    card.identity.grounded_impression,
    card.inner_core.core_contradiction,
    card.voiceprint.speech_style
  ];
  const description = candidates.find((item) => item.trim()) ?? "点击进入这张角色卡的五段式编辑。";
  return description.length > 72 ? `${description.slice(0, 72)}...` : description;
}
