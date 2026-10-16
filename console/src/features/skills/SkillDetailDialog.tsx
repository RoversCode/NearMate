import {
  AlertTriangle,
  ArrowDownToLine,
  ArrowUpFromLine,
  CheckCircle2,
  FileText,
  FolderOpen,
  ListChecks,
  RefreshCw
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { Skill, SkillDetail, SkillDependencyStatus } from "@/api/contracts/skills";
import { fetchSkillDetail } from "@/api/services/skills";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from "@/components/ui/dialog";

import {
  formatSkillDependencyState,
  formatSkillSource,
  formatSkillUsageState
} from "./skillPresentation";

/**
 * 描述 Skill 详情弹窗读取数据和关闭所需的输入。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   skill: 列表里已经可信的 Skill 摘要。
 *   onClose: 用户关闭弹窗后的页面状态更新动作。
 */
type SkillDetailDialogProps = {
  baseUrl: string;
  skill: Skill;
  onClose(): void;
};

/**
 * 按需读取并展示一个 Skill 的详情。
 *
 * Args:
 *   baseUrl: Backend 同源根地址。
 *   skill: 列表里已经可信的 Skill 摘要，用于弹窗立即呈现标题。
 *   onClose: 用户关闭弹窗后的导航动作。
 *
 * Returns:
 *   包含依赖、资产清单和纯文本 SKILL.md 的可访问弹窗。
 */
export function SkillDetailDialog({ baseUrl, skill, onClose }: SkillDetailDialogProps): JSX.Element {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [retrySequence, setRetrySequence] = useState(0);
  const displayedSkill = detail ?? skill;

  useEffect(() => {
    const abortController = new AbortController();
    setDetail(null);
    setErrorMessage(null);

    void fetchSkillDetail(baseUrl, skill.id, abortController.signal)
      .then(nextDetail => {
        if (!abortController.signal.aborted) {
          setDetail(nextDetail);
        }
      })
      .catch((error: unknown) => {
        if (abortController.signal.aborted) {
          return;
        }
        setErrorMessage(error instanceof Error ? error.message : "读取 Skill 详情失败");
      });

    return () => abortController.abort();
  }, [baseUrl, retrySequence, skill.id]);

  return (
    <Dialog open onOpenChange={open => { if (!open) onClose(); }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-3xl overflow-y-auto p-0 sm:w-full">
        <DialogHeader className="border-b px-6 pb-5 pt-6 pr-14">
          <div className="mb-1 flex flex-wrap items-center gap-2">
            <Badge className="max-w-full whitespace-normal break-words" variant="secondary">{displayedSkill.category}</Badge>
            <Badge variant={displayedSkill.ready ? "success" : "warning"}>
              {formatSkillDependencyState(displayedSkill)}
            </Badge>
          </div>
          <DialogTitle className="break-words">{displayedSkill.name}</DialogTitle>
          <DialogDescription className="break-words">{displayedSkill.summary}</DialogDescription>
        </DialogHeader>

        <div className="space-y-6 px-6 pb-6">
          <dl className="grid gap-3 border-b pb-5 text-sm sm:grid-cols-3">
            <MetadataItem label="版本" value={`v${displayedSkill.version}`} />
            <MetadataItem label="来源" value={formatSkillSource(displayedSkill.source)} />
            <MetadataItem label="任务状态" value={formatSkillUsageState(displayedSkill)} />
          </dl>

          {detail === null && errorMessage === null ? (
            <p className="py-10 text-center text-sm text-muted-foreground" role="status">正在读取 Skill 详情，请稍候。</p>
          ) : null}

          {errorMessage !== null ? (
            <div
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3"
              role="alert"
            >
              <p className="min-w-0 flex-1 break-words text-sm text-destructive">{errorMessage}</p>
              <Button
                onClick={() => setRetrySequence(sequence => sequence + 1)}
                size="sm"
                type="button"
                variant="outline"
              >
                <RefreshCw aria-hidden="true" />
                重试
              </Button>
            </div>
          ) : null}

          {detail !== null ? <SkillDetailContent detail={detail} /> : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * 渲染详情接口返回的能力说明、依赖、资产和说明原文。
 *
 * Args:
 *   detail: 已通过完整契约校验的 Skill 详情。
 *
 * Returns:
 *   只使用普通文本节点展示清单和 SKILL.md 的详情正文。
 */
function SkillDetailContent({ detail }: { detail: SkillDetail }): JSX.Element {
  const assetGroups = useMemo(() => groupSkillFiles(detail.files), [detail.files]);

  return (
    <>
      <section className="space-y-3">
        <h3 className="text-sm font-semibold">能力说明</h3>
        <div className="grid gap-5 sm:grid-cols-3">
          <SkillMetadataList
            icon={ListChecks}
            items={detail.trigger_scenarios}
            label="适用场景"
          />
          <SkillMetadataList
            icon={ArrowDownToLine}
            items={detail.inputs}
            label="输入"
          />
          <SkillMetadataList
            icon={ArrowUpFromLine}
            items={detail.outputs}
            label="输出"
          />
        </div>
      </section>

      <section className="space-y-3">
        <h3 className="text-sm font-semibold">运行依赖</h3>
        {detail.dependency_status.length === 0 ? (
          <p className="text-sm text-muted-foreground">此 Skill 不依赖额外的系统命令。</p>
        ) : (
          <div className="divide-y rounded-lg border">
            {detail.dependency_status.map(dependency => (
              <DependencyRow dependency={dependency} key={`${dependency.kind}:${dependency.name}`} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex items-center gap-2">
          <FolderOpen aria-hidden="true" className="size-4 text-muted-foreground" />
          <h3 className="text-sm font-semibold">目录资产</h3>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          {assetGroups.length === 0 ? (
            <p className="text-sm text-muted-foreground">当前没有可展示的目录资产。</p>
          ) : assetGroups.map(group => (
            <div className="min-w-0 space-y-2" key={group.label}>
              <p className="text-xs font-semibold text-muted-foreground">{group.label}</p>
              <ul className="space-y-1.5">
                {group.files.map(file => (
                  <li className="break-all rounded-md bg-muted px-2.5 py-1.5 font-mono text-xs" key={file}>
                    {file}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </section>

      <details className="group rounded-lg border">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <FileText aria-hidden="true" className="size-4 text-muted-foreground" />
          查看 SKILL.md 原文
        </summary>
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words border-t bg-muted/45 px-4 py-4 font-mono text-xs leading-relaxed">
          {detail.instruction}
        </pre>
      </details>
    </>
  );
}

/**
 * 渲染一个依赖命令的检查结果和本机版本。
 *
 * Args:
 *   dependency: Backend 已完成预检的单项外部依赖。
 *
 * Returns:
 *   包含可用状态、版本、路径和手动安装提示的依赖行。
 */
function DependencyRow({ dependency }: { dependency: SkillDependencyStatus }): JSX.Element {
  const StatusIcon = dependency.ready ? CheckCircle2 : AlertTriangle;
  return (
    <div className="flex gap-3 px-4 py-3">
      <StatusIcon
        aria-hidden="true"
        className={dependency.ready ? "mt-0.5 size-4 shrink-0 text-success" : "mt-0.5 size-4 shrink-0 text-warning"}
      />
      <div className="min-w-0 space-y-1">
        <p className="break-all text-sm font-medium">
          {dependency.name}
          {dependency.minimum_version !== undefined ? ` >= ${dependency.minimum_version}` : ""}
        </p>
        <p className="break-words text-xs leading-relaxed text-muted-foreground">
          {dependency.ready
            ? dependency.detected_version === null
              ? "命令已找到"
              : `已检测到版本 ${dependency.detected_version}`
            : dependency.reason}
        </p>
        {!dependency.ready ? (
          <p className="break-words text-xs leading-relaxed text-warning">
            请自行安装 {dependency.name} 并确认该命令已加入系统 PATH；NearMate 不会自动修改本机环境。
          </p>
        ) : null}
        {dependency.path !== null ? (
          <p className="break-all font-mono text-[0.68rem] text-muted-foreground">{dependency.path}</p>
        ) : null}
      </div>
    </div>
  );
}

/**
 * 描述一列结构化能力说明。
 *
 * Args:
 *   icon: 适用场景、输入或输出对应的 Lucide 图标。
 *   items: manifest 明确声明的展示文字。
 *   label: 当前列的短标题。
 */
type SkillMetadataListProps = {
  icon: typeof ListChecks;
  items: string[];
  label: string;
};

/**
 * 展示清单明确声明的一组场景、输入或输出。
 *
 * Args:
 *   icon: 当前分组使用的图标。
 *   items: 保持 manifest 顺序的展示文字。
 *   label: 分组标题。
 *
 * Returns:
 *   有内容时为语义列表，没有内容时为明确空态。
 */
function SkillMetadataList({ icon: Icon, items, label }: SkillMetadataListProps): JSX.Element {
  return (
    <div className="min-w-0 space-y-2">
      <div className="flex items-center gap-2 text-xs font-semibold text-muted-foreground">
        <Icon aria-hidden="true" className="size-4 shrink-0" />
        <span>{label}</span>
      </div>
      {items.length > 0 ? (
        <ul className="space-y-1.5 break-words text-sm leading-relaxed">
          {items.map(item => <li key={item}>{item}</li>)}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">未单独声明</p>
      )}
    </div>
  );
}

/**
 * 渲染一项只读 Skill 元数据。
 *
 * Args:
 *   label: 元数据名称。
 *   value: 必须完整展示的元数据值。
 *
 * Returns:
 *   可以随窄屏换行的定义列表项。
 */
function MetadataItem({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-medium">{value}</dd>
    </div>
  );
}

/**
 * 描述详情页按目录用途整理后的一组相对文件路径。
 *
 * Args:
 *   label: 脚本、模板、参考资料或基础文件分组名称。
 *   files: 属于该分组的安全相对路径。
 */
type SkillFileGroup = {
  label: string;
  files: string[];
};

/**
 * 把扁平相对路径整理成脚本、模板、参考资料和基础文件四组。
 *
 * Args:
 *   files: Backend 返回并已校验安全的相对文件路径。
 *
 * Returns:
 *   只包含非空分组、且每个文件恰好出现一次的展示数组。
 */
function groupSkillFiles(files: string[]): SkillFileGroup[] {
  const definitions = [
    { prefix: "scripts/", label: "脚本" },
    { prefix: "templates/", label: "模板" },
    { prefix: "references/", label: "参考资料" }
  ];
  const groups = definitions
    .map(definition => ({
      label: definition.label,
      files: files.filter(file => file.startsWith(definition.prefix))
    }))
    .filter(group => group.files.length > 0);
  const groupedFiles = new Set(groups.flatMap(group => group.files));
  const basicFiles = files.filter(file => !groupedFiles.has(file));
  if (basicFiles.length > 0) {
    groups.push({ label: "基础文件", files: basicFiles });
  }
  return groups;
}
