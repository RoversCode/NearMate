import { Blocks, CheckCircle2, LoaderCircle, Search, ToggleRight, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { Skill, SkillLibrarySummary } from "@/api/contracts/skills";
import type { ConsoleSettings } from "@/api/contracts/settings";
import { EmptyState } from "@/components/layout/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

import { SkillDetailDialog } from "./SkillDetailDialog";
import { SkillList } from "./SkillList";
import { useSkillLibrary } from "./useSkillLibrary";

/**
 * 描述技能库页面连接 Backend 所需的输入。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 */
type SkillLibraryPageProps = {
  baseUrl: string;
  /** Skill 保存成功后刷新 Console 共享设置版本。 */
  onSettingsSnapshotRefresh?: (candidateSettings?: ConsoleSettings) => Promise<unknown>;
};

/**
 * 渲染 NearMate 后台工作拥有的 Skill 库。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *
 * Returns:
 *   包含汇总、搜索、分类筛选、详情和全局开关的独立页面。
 */
export function SkillLibraryPage({ baseUrl, onSettingsSnapshotRefresh }: SkillLibraryPageProps): JSX.Element {
  const controller = useSkillLibrary(baseUrl, onSettingsSnapshotRefresh);
  const [searchText, setSearchText] = useState("");
  const [selectedCategory, setSelectedCategory] = useState("all");
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);
  const skills = controller.snapshot?.skills ?? [];
  const categories = useMemo(
    () => Array.from(new Set(skills.map(skill => skill.category))).sort((left, right) => left.localeCompare(right, "zh-CN")),
    [skills]
  );

  useEffect(() => {
    // 清单刷新后分类可能被移除；回到“全部”能避免原生 select 显示空值却筛出空列表。
    if (selectedCategory !== "all" && !categories.includes(selectedCategory)) {
      setSelectedCategory("all");
    }
  }, [categories, selectedCategory]);

  const filteredSkills = useMemo(
    () => filterSkills(skills, searchText, selectedCategory),
    [searchText, selectedCategory, skills]
  );
  const selectedSkill = skills.find(skill => skill.id === selectedSkillId) ?? null;

  return (
    <section aria-busy={controller.requestState === "loading"} className="space-y-5">
      <PageHeader
        actions={controller.snapshot !== null && controller.requestState === "loading" ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground" role="status">
            <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />
            正在同步
          </span>
        ) : undefined}
        description="查看并控制 NearMate 后台工作可使用的产品 Skill。开关只影响之后新建的任务。"
        title="技能库"
      />

      {controller.snapshot !== null ? (
        <SkillSummary summary={controller.snapshot.summary} />
      ) : null}

      {controller.updateErrorMessage !== null ? (
        <InlineError message={controller.updateErrorMessage} onRetry={controller.retry} />
      ) : null}
      {controller.updateWarningMessage !== null ? (
        <div className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3" role="status">
          <p className="break-words text-sm text-warning">{controller.updateWarningMessage}</p>
        </div>
      ) : null}

      {controller.snapshot !== null && controller.snapshot.issues.length > 0 ? (
        <div className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3" role="alert">
          <p className="break-words text-sm font-medium text-warning">
            {controller.snapshot.issues.length} 个 Skill 清单未能加载
          </p>
          <ul className="mt-1 space-y-1 break-words text-xs text-muted-foreground">
            {controller.snapshot.issues.map(issue => (
              <li key={`${issue.name}:${issue.message}`}>{issue.name}：{issue.message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {controller.snapshot !== null ? (
        <div className="flex flex-col gap-3 rounded-lg border bg-card p-3 sm:flex-row sm:items-center">
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">搜索 Skill</span>
            <Search aria-hidden="true" className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="pl-9"
              onChange={event => setSearchText(event.target.value)}
              placeholder="搜索名称、能力、分类或 ID"
              type="search"
              value={searchText}
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-muted-foreground sm:w-56">
            <span className="shrink-0">分类</span>
            <Select
              aria-label="Skill 分类"
              onChange={event => setSelectedCategory(event.target.value)}
              value={selectedCategory}
            >
              <option value="all">全部分类</option>
              {categories.map(category => (
                <option key={category} value={category}>{category}</option>
              ))}
            </Select>
          </label>
        </div>
      ) : null}

      {controller.snapshot === null && controller.requestState === "loading" ? (
        <EmptyState icon={Blocks}>正在读取技能库，请稍候。</EmptyState>
      ) : null}

      {controller.snapshot === null && controller.requestState === "failed" ? (
        <InlineError
          message={controller.loadErrorMessage ?? "读取技能库失败"}
          onRetry={controller.retry}
        />
      ) : null}

      {controller.snapshot !== null && skills.length === 0 ? (
        <EmptyState icon={Blocks}>当前还没有可展示的产品 Skill。</EmptyState>
      ) : null}

      {controller.snapshot !== null && skills.length > 0 && filteredSkills.length === 0 ? (
        <EmptyState icon={Search}><p>没有符合当前搜索和分类条件的 Skill。</p><Button className="mt-3" onClick={() => { setSearchText(""); setSelectedCategory("all"); }} variant="outline">清空筛选</Button></EmptyState>
      ) : null}

      {filteredSkills.length > 0 ? (
        <SkillList
          onOpenSkill={skill => setSelectedSkillId(skill.id)}
          onToggleSkill={(skillId, enabled) => { void controller.setSkillEnabled(skillId, enabled); }}
          areTogglesDisabled={controller.requestState !== "ready"}
          skills={filteredSkills}
          updatingSkillId={controller.updatingSkillId}
        />
      ) : null}

      {selectedSkill !== null ? (
        <SkillDetailDialog
          baseUrl={baseUrl}
          onClose={() => setSelectedSkillId(null)}
          skill={selectedSkill}
        />
      ) : null}
    </section>
  );
}

/**
 * 渲染总数、偏好开启、下一任务可用和依赖缺失四项总览。
 *
 * Args:
 *   summary: 已与 Skill 列表核对一致的数量汇总。
 *
 * Returns:
 *   在手机、平板和桌面分别使用一、二、四列的定义列表。
 */
function SkillSummary({ summary }: { summary: SkillLibrarySummary }): JSX.Element {
  const items = [
    { label: "全部 Skill", value: summary.total, icon: Blocks, className: "text-primary" },
    { label: "已开启", value: summary.enabled, icon: ToggleRight, className: "text-foreground" },
    { label: "下一任务可用", value: summary.usable, icon: CheckCircle2, className: "text-success" },
    { label: "依赖缺失", value: summary.total - summary.ready, icon: TriangleAlert, className: "text-warning" }
  ];

  return (
    <dl className="grid overflow-hidden rounded-lg border bg-card sm:grid-cols-2 lg:grid-cols-4">
      {items.map((item, index) => {
        const Icon = item.icon;
        const dividerClassName = [
          "",
          "border-t sm:border-l sm:border-t-0",
          "border-t lg:border-l lg:border-t-0",
          "border-t sm:border-l lg:border-t-0"
        ][index];
        return (
          <div
            className={`flex items-center gap-3 px-4 py-3 ${dividerClassName}`}
            key={item.label}
          >
            <Icon aria-hidden="true" className={`size-4 shrink-0 ${item.className}`} />
            <div>
              <dt className="text-xs text-muted-foreground">{item.label}</dt>
              <dd className="text-lg font-semibold tabular-nums">{item.value}</dd>
            </div>
          </div>
        );
      })}
    </dl>
  );
}

/**
 * 渲染保留当前页面上下文的可重试错误。
 *
 * Args:
 *   message: 面向用户展示的失败原因。
 *   onRetry: 用户点击重试后的重新读取动作。
 *
 * Returns:
 *   能被读屏立即播报且长错误文字可以换行的错误条。
 */
function InlineError({ message, onRetry }: { message: string; onRetry(): void }): JSX.Element {
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3"
      role="alert"
    >
      <p className="min-w-0 flex-1 break-words text-sm text-destructive">{message}</p>
      <Button onClick={onRetry} size="sm" type="button" variant="outline">重试</Button>
    </div>
  );
}

/**
 * 根据搜索词和分类得到稳定顺序的可见 Skill。
 *
 * Args:
 *   skills: Backend 已排好顺序的完整 Skill 列表。
 *   searchText: 用户输入的名称、能力或 ID 关键词。
 *   category: 当前分类；all 表示不限制分类。
 *
 * Returns:
 *   保留原顺序并匹配摘要、场景、输入和输出的 Skill 列表。
 */
function filterSkills(skills: Skill[], searchText: string, category: string): Skill[] {
  const normalizedSearch = searchText.trim().toLocaleLowerCase("zh-CN");
  return skills.filter(skill => {
    if (category !== "all" && skill.category !== category) {
      return false;
    }
    if (!normalizedSearch) {
      return true;
    }
    return [
      skill.name,
      skill.summary,
      skill.category,
      skill.id,
      ...skill.trigger_scenarios,
      ...skill.inputs,
      ...skill.outputs
    ]
      .some(value => value.toLocaleLowerCase("zh-CN").includes(normalizedSearch));
  });
}
