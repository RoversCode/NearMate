import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import {
  type ToolWaitingFamily,
  type ToolWaitingPoliciesPayload,
  type ToolWaitingTool
} from "../../api/contracts/toolWaitingPolicies";
import {
  fetchToolWaitingPolicies,
  updateToolWaitingFamilyPolicy,
  updateToolWaitingToolPolicy
} from "../../api/services/toolWaitingPolicies";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";

/**
 * 展示工具等待句的启用状态，展开后编辑分类默认值和单个工具例外。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *
 * Returns:
 *   紧凑的策略列表，包含保存反馈、读取失败重试和独立编辑草稿。
 */
export function ToolWaitingPolicyPanel({ baseUrl }: { baseUrl: string }): JSX.Element {
  const panelId = useId();
  const [payload, setPayload] = useState<ToolWaitingPoliciesPayload | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [expandedFamilies, setExpandedFamilies] = useState<Set<string>>(new Set());
  // 草稿只存放用户改过的字段，其他项目继续跟随服务端快照；保存一项不会重置其他编辑。
  const [familyDrafts, setFamilyDrafts] = useState<Record<string, string>>({});
  const [toolDrafts, setToolDrafts] = useState<Record<string, string>>({});
  const [customTools, setCustomTools] = useState<Set<string>>(new Set());
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const saveInProgress = useRef(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [savedMessage, setSavedMessage] = useState<string | null>(null);

  useEffect(() => {
    let isDisposed = false;
    setPayload(null);
    setErrorMessage(null);
    setSavedMessage(null);
    setFamilyDrafts({});
    setToolDrafts({});
    setCustomTools(new Set());
    void fetchToolWaitingPolicies(baseUrl)
      .then((nextPayload) => {
        if (!isDisposed) setPayload(nextPayload);
      })
      .catch((error: unknown) => {
        if (!isDisposed) {
          setErrorMessage(error instanceof Error ? error.message : "读取工具等待策略失败");
        }
      });
    return () => { isDisposed = true; };
  }, [baseUrl, loadAttempt]);

  /**
   * 保存分类策略，开关只更改启停状态，文案按钮才提交当前草稿。
   *
   * Args:
   *   family: 当前服务端分类快照。
   *   enabled: 保存后的启停状态，默认保持现状。
   *   saveDraft: 是否同时提交并清除该分类的文案草稿。
   *
   * Returns:
   *   保存结束后更新权威快照和操作反馈。
   */
  async function saveFamily(
    family: ToolWaitingFamily,
    enabled = family.effective_policy.enabled,
    saveDraft = true
  ): Promise<void> {
    if (saveInProgress.current) return;
    saveInProgress.current = true;
    setBusyKey(family.family_id);
    setErrorMessage(null);
    setSavedMessage(null);
    try {
      const phrases = saveDraft
        ? phrasesFromDraft(familyDrafts[family.family_id] ?? family.effective_policy.phrases.join("\n"))
        : family.effective_policy.phrases;
      const nextPayload = await updateToolWaitingFamilyPolicy(baseUrl, family.family_id, { enabled, phrases });
      setPayload(nextPayload);
      if (saveDraft) {
        setFamilyDrafts((current) => {
          const next = { ...current };
          delete next[family.family_id];
          return next;
        });
      }
      setSavedMessage(`${family.display_name}${saveDraft ? "等待句已保存" : enabled ? "等待反馈已开启" : "等待反馈已关闭"}。`);
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "保存工具等待策略失败");
    } finally {
      saveInProgress.current = false;
      setBusyKey(null);
    }
  }

  /**
   * 保存单工具文案或恢复继承分类设置，只清除当前工具的草稿。
   *
   * Args:
   *   tool: 当前服务端工具快照。
   *   inheritFamily: 是否移除单独配置并恢复继承。
   *
   * Returns:
   *   保存结束后刷新策略，并保留所有其他未提交编辑。
   */
  async function saveTool(tool: ToolWaitingTool, inheritFamily: boolean): Promise<void> {
    if (saveInProgress.current) return;
    saveInProgress.current = true;
    setBusyKey(tool.name);
    setErrorMessage(null);
    setSavedMessage(null);
    try {
      const nextPayload = await updateToolWaitingToolPolicy(baseUrl, tool.name, inheritFamily
        ? { inherit_family: true }
        : {
            inherit_family: false,
            enabled: tool.user_override?.enabled ?? true,
            phrases: phrasesFromDraft(toolDrafts[tool.name] ?? tool.effective_policy.phrases.join("\n"))
          });
      setPayload(nextPayload);
      setToolDrafts((current) => {
        const next = { ...current };
        delete next[tool.name];
        return next;
      });
      setCustomTools((current) => {
        const next = new Set(current);
        next.delete(tool.name);
        return next;
      });
      setSavedMessage(`${tool.display_name}${inheritFamily ? "已恢复继承分类设置" : "等待句已保存"}。`);
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "保存工具等待策略失败");
    } finally {
      saveInProgress.current = false;
      setBusyKey(null);
    }
  }

  const visibleFamilies = payload?.families.filter((family) => family.latency_class !== "instant") ?? [];
  const isSaving = busyKey !== null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>工具等待反馈</CardTitle>
        <CardDescription>工具请求期间先播放一句短回应。开关立即保存，文案编辑后单独保存。</CardDescription>
      </CardHeader>
      <CardContent>
        {errorMessage !== null ? (
          <div className="mb-3 flex items-center justify-between gap-3 text-sm text-destructive" role="alert">
            <p>{errorMessage}</p>
            {payload === null ? <Button onClick={() => setLoadAttempt((current) => current + 1)} size="sm" type="button" variant="outline">重试</Button> : null}
          </div>
        ) : null}
        {savedMessage !== null ? <p className="mb-3 text-sm text-muted-foreground" role="status">{savedMessage}</p> : null}
        {payload === null && errorMessage === null ? (
          <p className="py-2 text-sm text-muted-foreground" role="status">正在读取工具等待策略…</p>
        ) : null}
        {payload !== null && visibleFamilies.length === 0 ? (
          <p className="py-2 text-sm text-muted-foreground">暂无需要等待反馈的工具。</p>
        ) : null}
        <div className="divide-y">
          {visibleFamilies.map((family) => {
            const isExpanded = expandedFamilies.has(family.family_id);
            const editorId = `${panelId}-${family.family_id}`;
            const savedPhrases = family.effective_policy.phrases.join("\n");
            const familyDraft = familyDrafts[family.family_id] ?? savedPhrases;
            const familyIsDirty = familyDraft !== savedPhrases;
            const visibleTools = family.tools.filter((tool) => tool.latency_class !== "instant");
            return (
              <section key={family.family_id}>
                <div className="flex flex-wrap items-center justify-between gap-3 py-3">
                  <div className="min-w-0 flex-1">
                    <h4 className="text-sm font-medium">{family.display_name}</h4>
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">
                      {familyIsDirty ? "有未保存的文案" : family.effective_policy.phrases[0] || "尚未设置等待句"}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <span className="text-xs text-muted-foreground">{busyKey === family.family_id ? "保存中…" : family.effective_policy.enabled ? "已开启" : "已关闭"}</span>
                    <Switch
                      aria-label={`${family.effective_policy.enabled ? "关闭" : "开启"}${family.display_name}等待句播报`}
                      checked={family.effective_policy.enabled}
                      disabled={isSaving}
                      onCheckedChange={(checked) => void saveFamily(family, checked, false)}
                    />
                    <Button
                      aria-controls={editorId}
                      aria-expanded={isExpanded}
                      aria-label={`${isExpanded ? "收起" : "编辑"}${family.display_name}等待反馈`}
                      onClick={() => setExpandedFamilies((current) => {
                        const next = new Set(current);
                        if (next.has(family.family_id)) next.delete(family.family_id);
                        else next.add(family.family_id);
                        return next;
                      })}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      {isExpanded ? "收起" : "编辑"}
                      {isExpanded ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}
                    </Button>
                  </div>
                </div>
                {isExpanded ? (
                  <div className="space-y-3 border-t border-dashed pb-4 pt-3" id={editorId}>
                    <label className="block text-xs text-muted-foreground" htmlFor={`${editorId}-phrases`}>分类默认等待句 · 每行一句</label>
                    <Textarea
                      aria-label={`${family.display_name}等待句`}
                      disabled={isSaving}
                      id={`${editorId}-phrases`}
                      onChange={(event) => {
                        setFamilyDrafts((current) => ({ ...current, [family.family_id]: event.target.value }));
                        setSavedMessage(null);
                      }}
                      rows={2}
                      value={familyDraft}
                    />
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-xs text-muted-foreground">{familyIsDirty ? "未保存" : "与已保存文案一致"}</span>
                      <Button disabled={isSaving || !familyIsDirty} onClick={() => void saveFamily(family)} size="sm" type="button">
                        {busyKey === family.family_id ? "保存中…" : `保存${family.display_name}策略`}
                      </Button>
                    </div>
                    {visibleTools.length > 0 ? (
                      <details className="border-t pt-3">
                        <summary className="cursor-pointer text-xs font-medium">单独设置工具（{visibleTools.length}）</summary>
                        <div className="mt-2 divide-y">
                          {visibleTools.map((tool) => {
                            const isCustom = customTools.has(tool.name) || tool.user_override !== null;
                            const savedToolPhrases = tool.effective_policy.phrases.join("\n");
                            const draft = toolDrafts[tool.name] ?? savedToolPhrases;
                            const toolIsDirty = draft !== savedToolPhrases || customTools.has(tool.name);
                            return (
                              <div className="space-y-2 py-3" key={tool.name}>
                                <div className="flex items-center justify-between gap-3">
                                  <div>
                                    <h5 className="text-sm font-medium">{tool.display_name}</h5>
                                    <p className="text-xs text-muted-foreground">{isCustom ? tool.user_override?.enabled === false ? "单独配置 · 已关闭" : "单独配置" : `继承${family.display_name}设置`}</p>
                                  </div>
                                  <Button
                                    disabled={isSaving}
                                    onClick={() => {
                                      if (isCustom) { void saveTool(tool, true); return; }
                                      setCustomTools((current) => new Set([...current, tool.name]));
                                      setSavedMessage(null);
                                    }}
                                    size="sm"
                                    type="button"
                                    variant="outline"
                                  >
                                    {isCustom ? `继承${family.display_name}` : `自定义${tool.display_name}`}
                                  </Button>
                                </div>
                                {isCustom ? (
                                  <>
                                    <Textarea
                                      aria-label={`${tool.display_name}等待句`}
                                      disabled={isSaving}
                                      onChange={(event) => {
                                        setToolDrafts((current) => ({ ...current, [tool.name]: event.target.value }));
                                        setSavedMessage(null);
                                      }}
                                      rows={2}
                                      value={draft}
                                    />
                                    <div className="flex items-center justify-between gap-3">
                                      <span className="text-xs text-muted-foreground">{toolIsDirty ? "未保存" : "与已保存文案一致"}</span>
                                      <Button disabled={isSaving || !toolIsDirty} onClick={() => void saveTool(tool, false)} size="sm" type="button">
                                        {busyKey === tool.name ? "保存中…" : `保存${tool.display_name}策略`}
                                      </Button>
                                    </div>
                                  </>
                                ) : null}
                              </div>
                            );
                          })}
                        </div>
                      </details>
                    ) : null}
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * 把逐行编辑的等待句整理成服务端列表。
 *
 * Args:
 *   value: 文本框中的完整草稿。
 *
 * Returns:
 *   去除首尾空白和空行后的等待句。
 */
function phrasesFromDraft(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter((line) => line.length > 0);
}
