import { AudioLines, Blocks, BookOpen, ChartNoAxesCombined, Drama, ListTodo, ScrollText, Settings2, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { consoleSections, type ConsoleSectionId } from "@/app/consoleTypes";
import { NEARMATE_BRAND_ICON_URL, NEARMATE_PRODUCT_NAME } from "@/brand/brandIdentity";
import { NEARMATE_HELP_LINKS } from "@/brand/helpLinks";

const sectionIconById: Record<ConsoleSectionId, LucideIcon> = {
  settings: Settings2,
  tasks: ListTodo,
  skills: Blocks,
  usage: ChartNoAxesCombined,
  "character-cards": Drama,
  streamvox: AudioLines,
  logs: ScrollText
};

type AppSidebarProps = {
  activeSectionId: ConsoleSectionId;
  onSelectSection: (sectionId: ConsoleSectionId) => void;
  restrictToStreamVox?: boolean;
};

/**
 * 控制台侧边导航：品牌标识 + 图标化页面导航。
 *
 * Args:
 *   activeSectionId: 当前激活的页面。
 *   onSelectSection: 切换页面回调。
 *   restrictToStreamVox: 语音未就绪时只开放模型管理，避免向已暂停的助手提交操作。
 *
 * Returns:
 *   桌面端为固定侧栏，移动端退化为顶部横向导航条。
 */
export function AppSidebar({ activeSectionId, onSelectSection, restrictToStreamVox = false }: AppSidebarProps): JSX.Element {
  return (
    <aside className="flex shrink-0 flex-col gap-7 border-b bg-card px-3 py-4 lg:sticky lg:top-0 lg:h-screen lg:w-52 lg:border-b-0 lg:border-r lg:py-6">
      <div className="flex items-center gap-3 px-1">
        <img
          alt=""
          aria-hidden="true"
          className="size-9 rounded-xl object-cover shadow-md"
          draggable="false"
          src={NEARMATE_BRAND_ICON_URL}
        />
        <div className="leading-tight">
          <h1 className="text-base font-bold">{NEARMATE_PRODUCT_NAME}</h1>
          <p className="mt-1 text-[11px] text-muted-foreground">桌面工作搭子</p>
        </div>
      </div>

      <nav aria-label="控制台页面" className="flex gap-1 overflow-x-auto lg:flex-col lg:overflow-visible">
        {consoleSections.map((section) => {
          const Icon = sectionIconById[section.id];
          const isActive = section.id === activeSectionId;

          return (
            <button
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40",
                isActive
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/60 hover:text-foreground"
              )}
              key={section.id}
              disabled={restrictToStreamVox && section.id !== "streamvox"}
              onClick={() => onSelectSection(section.id)}
              type="button"
            >
              <Icon aria-hidden="true" className="size-4" />
              {section.label}
            </button>
          );
        })}
      </nav>
      {/* 桌面左下仍有授权管理入口，给它留出空间，避免遮住使用手册。 */}
      <a className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium text-muted-foreground hover:bg-accent/60 hover:text-foreground lg:mb-12 lg:mt-auto"
        href={NEARMATE_HELP_LINKS.manual} rel="noopener noreferrer" target="_blank">
        <BookOpen aria-hidden="true" className="size-4" />使用手册
      </a>
    </aside>
  );
}
