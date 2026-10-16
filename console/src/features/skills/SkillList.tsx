import { ChevronRight } from "lucide-react";

import type { Skill } from "@/api/contracts/skills";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

import {
  formatSkillDependencyState,
  formatSkillSource,
  formatSkillUsageState
} from "./skillPresentation";

/**
 * 描述密集 Skill 列表的展示数据和用户动作。
 *
 * Args:
 *   skills: 已经过搜索和分类筛选的 Skill。
 *   updatingSkillId: 当前正在保存的 Skill ID。
 *   areTogglesDisabled: 列表刷新等情况下是否统一禁止提交开关。
 *   onOpenSkill: 点击名称时打开详情。
 *   onToggleSkill: 用户切换启用偏好时提交新状态。
 */
type SkillListProps = {
  skills: Skill[];
  updatingSkillId: string | null;
  areTogglesDisabled: boolean;
  onOpenSkill(skill: Skill): void;
  onToggleSkill(skillId: string, enabled: boolean): void;
};

/**
 * 渲染适合 30 个以上 Skill 扫描和操作的密集列表。
 *
 * Args:
 *   skills: 已经过搜索和分类筛选的 Skill。
 *   updatingSkillId: 当前正在保存的 Skill ID；有值时串行锁住全部开关。
 *   areTogglesDisabled: 当前列表快照尚未稳定时是否统一锁住开关。
 *   onOpenSkill: 点击名称时打开详情。
 *   onToggleSkill: 用户切换启用偏好时提交新状态。
 *
 * Returns:
 *   桌面端使用稳定列宽，窄屏自动堆叠且保持开关可见的列表。
 */
export function SkillList({
  skills,
  updatingSkillId,
  areTogglesDisabled,
  onOpenSkill,
  onToggleSkill
}: SkillListProps): JSX.Element {
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div
        aria-hidden="true"
        className="hidden grid-cols-[minmax(0,1.6fr)_minmax(8.5rem,0.55fr)_minmax(10rem,0.65fr)_7rem] gap-4 border-b bg-muted/45 px-4 py-2.5 text-xs font-semibold text-muted-foreground lg:grid"
      >
        <span>Skill</span>
        <span>分类与来源</span>
        <span>运行状态</span>
        <span className="text-right">启用</span>
      </div>

      <div className="divide-y">
        {skills.map(skill => (
          <SkillListRow
            key={skill.id}
            onOpenSkill={onOpenSkill}
            onToggleSkill={onToggleSkill}
            skill={skill}
            areTogglesDisabled={areTogglesDisabled}
            updatingSkillId={updatingSkillId}
          />
        ))}
      </div>
    </div>
  );
}

/**
 * 描述单条 Skill 行在列表公共输入之外需要的当前 Skill。
 *
 * Args:
 *   skill: 当前行展示和操作的完整 Skill 状态。
 */
type SkillListRowProps = Omit<SkillListProps, "skills"> & {
  skill: Skill;
};

/**
 * 渲染一条包含摘要、依赖和全局开关的 Skill 状态。
 *
 * Args:
 *   skill: 当前行的完整 Skill 状态。
 *   updatingSkillId: 当前正在提交的 Skill ID。
 *   areTogglesDisabled: 当前快照不稳定时统一锁住开关。
 *   onOpenSkill: 打开当前 Skill 详情的动作。
 *   onToggleSkill: 提交当前 Skill 新开关值的动作。
 *
 * Returns:
 *   可在窄屏堆叠、并为开关关联状态说明的列表行。
 */
function SkillListRow({
  skill,
  updatingSkillId,
  areTogglesDisabled,
  onOpenSkill,
  onToggleSkill
}: SkillListRowProps): JSX.Element {
  const isAnySkillUpdating = updatingSkillId !== null;
  const isThisSkillUpdating = updatingSkillId === skill.id;
  const statusDescriptionId = `skill-${skill.id}-status`;

  return (
    <article
      aria-busy={isThisSkillUpdating || undefined}
      className="grid gap-4 px-4 py-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(8.5rem,0.55fr)_minmax(10rem,0.65fr)_7rem] lg:items-center"
    >
      <div className="min-w-0 space-y-1.5">
        <button
          aria-label={`查看 ${skill.name} 详情`}
          className="group flex max-w-full items-center gap-1.5 text-left font-semibold text-foreground outline-none hover:text-primary focus-visible:text-primary focus-visible:underline"
          onClick={() => onOpenSkill(skill)}
          title={skill.name}
          type="button"
        >
          <span className="truncate">{skill.name}</span>
          <ChevronRight aria-hidden="true" className="size-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </button>
        <p className="line-clamp-2 text-sm leading-relaxed text-muted-foreground">{skill.summary}</p>
        <p className="break-all font-mono text-[0.68rem] text-muted-foreground">{skill.id}</p>
      </div>

      <div className="flex flex-wrap items-center gap-1.5 lg:block lg:space-y-1.5">
        <Badge className="max-w-full whitespace-normal break-words" variant="secondary">{skill.category}</Badge>
        <p className="break-all text-xs text-muted-foreground">
          {formatSkillSource(skill.source)} · v{skill.version}
        </p>
      </div>

      <div className="min-w-0 space-y-1.5" id={statusDescriptionId}>
        <Badge variant={skill.ready ? "success" : "warning"}>
          {formatSkillDependencyState(skill)}
        </Badge>
        <p
          className={cn(
            "text-xs leading-relaxed",
            skill.usable ? "text-success" : "text-muted-foreground"
          )}
        >
          {formatSkillUsageState(skill)}
        </p>
        {!skill.ready && skill.unavailable_reason !== null ? (
          <p className="line-clamp-2 text-xs leading-relaxed text-warning">
            {skill.unavailable_reason}
          </p>
        ) : null}
      </div>

      <div className="flex items-center justify-between gap-3 lg:justify-end">
        <span className="text-xs text-muted-foreground lg:hidden">
          {isThisSkillUpdating ? "正在保存" : skill.enabled ? "已开启" : "已关闭"}
        </span>
        <Switch
          aria-describedby={statusDescriptionId}
          aria-label={isThisSkillUpdating
            ? `正在保存 ${skill.name}`
            : `${skill.enabled ? "关闭" : "开启"} ${skill.name}`}
          checked={skill.enabled}
          disabled={areTogglesDisabled || isAnySkillUpdating}
          onCheckedChange={checked => onToggleSkill(skill.id, checked)}
        />
      </div>
    </article>
  );
}
