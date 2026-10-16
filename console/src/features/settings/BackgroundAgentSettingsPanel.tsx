import { type FormEvent, useEffect, useRef, useState } from "react";

import type {
  BackgroundAgentConfig,
  BackgroundAgentCredentialType,
  BackgroundAgentEffortLevel,
  BackgroundAgentPermissionMode
} from "../../api/contracts/backgroundAgent";
import type { ConsoleSettings } from "../../api/contracts/settings";
import {
  fetchBackgroundAgentConfig,
  updateBackgroundAgentConfig
} from "../../api/services/backgroundAgent";
import { fetchSettings } from "../../api/services/settings";
import { formatSavedRefreshWarning } from "../../lib/mutationFeedback";
import { EmptyState } from "@/components/layout/EmptyState";
import { SettingRow } from "@/components/layout/SettingRow";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

const KNOWN_ENVIRONMENT_FIELDS = [
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_DEFAULT_OPUS_MODEL",
  "ANTHROPIC_DEFAULT_SONNET_MODEL",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL"
] as const;

const MODEL_ALIAS_FIELDS = KNOWN_ENVIRONMENT_FIELDS.slice(1);
const RESERVED_ADVANCED_ENVIRONMENT_KEYS = new Set([
  "ANTHROPIC_MODEL",
  "CLAUDE_CODE_EFFORT_LEVEL",
  "CLAUDE_CODE_SUBAGENT_MODEL"
]);

type KnownEnvironmentField = (typeof KNOWN_ENVIRONMENT_FIELDS)[number];
type EnvironmentDraft = Record<KnownEnvironmentField, string>;

/**
 * 渲染当前角色后台工作使用的 Claude Code 配置面板。
 *
 * Args:
 *   baseUrl: Backend 同源地址。
 *   onSettingsChange: 保存或读取配置后同步完整的普通设置快照。
 *
 * Returns:
 *   官方环境变量、独立凭据、任务护栏和可信权限模式表单。
 */
export function BackgroundAgentSettingsPanel({
  baseUrl,
  onSettingsChange
}: {
  baseUrl: string;
  /** 后台配置保存后，把提交点的完整设置快照同步回普通设置页。 */
  onSettingsChange?: (settings: ConsoleSettings) => void;
}): JSX.Element {
  const [config, setConfig] = useState<BackgroundAgentConfig | null>(null);
  const [environment, setEnvironment] = useState<EnvironmentDraft>(emptyEnvironmentDraft());
  const [advancedEnvironment, setAdvancedEnvironment] = useState("");
  const [credentialType, setCredentialType] = useState<BackgroundAgentCredentialType>("api_key");
  const [credentialValue, setCredentialValue] = useState("");
  const [model, setModel] = useState("");
  const [effortLevel, setEffortLevel] = useState<BackgroundAgentEffortLevel | "">("");
  const [maxTurns, setMaxTurns] = useState<number | "">(30);
  const [permissionMode, setPermissionMode] = useState<BackgroundAgentPermissionMode>("acceptEdits");
  const [isSaving, setIsSaving] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [savedMessage, setSavedMessage] = useState<string | null>(null);
  const [refreshWarningMessage, setRefreshWarningMessage] = useState<string | null>(null);
  const onSettingsChangeRef = useRef(onSettingsChange);

  useEffect(() => {
    // SettingsPage 每次渲染都会创建新的回调；单独保存最新引用，避免把它放进
    // 配置读取 effect 的依赖后因为普通设置更新而重复请求后台配置。
    onSettingsChangeRef.current = onSettingsChange;
  }, [onSettingsChange]);

  useEffect(() => {
    let cancelled = false;
    setErrorMessage(null);
    setRefreshWarningMessage(null);
    void fetchBackgroundAgentConfig(baseUrl)
      .then((loadedConfig) => {
        if (cancelled) {
          return;
        }
        applyConfigToDraft(loadedConfig);
        // 后台配置读取与普通设置读取可能乱序；响应里的完整快照是同一门禁下
        // 冻结的权威版本，必须先写回共享状态，避免随后保存普通设置携带旧 revision。
        if (loadedConfig.settings !== undefined) {
          onSettingsChangeRef.current?.(loadedConfig.settings);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setErrorMessage(error instanceof Error ? error.message : "读取后台工作配置失败");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl, loadAttempt]);

  /**
   * 把 Backend 的独立后台配置同步到表单草稿。
   *
   * Args:
   *   nextConfig: 不含凭据明文的后台配置快照。
   *
   * Returns:
   *   无返回值；更新表单并清空已经提交的凭据输入。
   */
  function applyConfigToDraft(nextConfig: BackgroundAgentConfig): void {
    setConfig(nextConfig);
    setEnvironment(
      Object.fromEntries(
        KNOWN_ENVIRONMENT_FIELDS.map((key) => [key, nextConfig.environment[key] ?? ""])
      ) as EnvironmentDraft
    );
    setAdvancedEnvironment(
      Object.entries(nextConfig.environment)
        .filter(([key]) => !KNOWN_ENVIRONMENT_FIELDS.includes(key as KnownEnvironmentField))
        .map(([key, value]) => `${key}=${value}`)
        .join("\n")
    );
    setCredentialType(nextConfig.credential_type);
    setCredentialValue("");
    // 空模型表示尚未配置。后台接入方可能和聊天服务不同，不能用 LLM 职责
    // 或可选别名补出主模型，必须保留用户明确保存的值。
    setModel(nextConfig.model.trim());
    setEffortLevel(nextConfig.effort_level ?? "");
    setMaxTurns(nextConfig.max_turns);
    setPermissionMode(nextConfig.permission_mode);
  }

  /**
   * 保存用户明确填写的后台配置，让新任务使用它。
   *
   * Args:
   *   event: 后台配置表单的提交事件。
   *
   * Returns:
   *   保存与设置快照同步结束后完成的 Promise。
   */
  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (isSaving) {
      return;
    }
    const normalizedModel = model.trim();
    if (!normalizedModel) {
      setErrorMessage("请填写后台工作主模型后再保存。");
      return;
    }
    if (!isValidMaxTurns(maxTurns)) {
      setErrorMessage("最大执行轮数必须是 0 到 200 之间的整数。");
      return;
    }
    setIsSaving(true);
    setErrorMessage(null);
    setSavedMessage(null);
    setRefreshWarningMessage(null);
    try {
      const saved = await updateBackgroundAgentConfig(baseUrl, {
        settings: {
          environment: {
            ...parseAdvancedEnvironment(advancedEnvironment),
            ...Object.fromEntries(
              Object.entries(environment).filter(([, value]) => value.trim().length > 0)
            )
          },
          model: normalizedModel,
          effort_level: effortLevel || null,
          max_turns: maxTurns,
          permission_mode: permissionMode
        },
        credential_type: credentialType,
        credential_value: credentialValue,
        clear_credential: false
      });
      applyConfigToDraft(saved);
      // 新 Backend 直接返回提交点快照；旧 Backend 没有该字段时再回读一次
      // 权威设置，避免只改 revision 而把并发更新的普通字段带回去。
      let committedSettings = saved.settings;
      if (committedSettings === undefined) {
        try {
          committedSettings = await fetchSettings(baseUrl);
        } catch (refreshError) {
          // 配置本身已经保存成功；把快照回读失败单独提示，避免用户误以为
          // 普通设置版本也已同步完成，下一次提交时才突然遇到 409。
          setRefreshWarningMessage(formatSavedRefreshWarning(refreshError, "设置快照暂时不可用"));
        }
      }
      if (committedSettings !== undefined) {
        onSettingsChange?.(committedSettings);
      }
      setSavedMessage("已保存；之后创建的新任务会使用这套配置，运行中的任务不受影响。");
    } catch (error: unknown) {
      setErrorMessage(error instanceof Error ? error.message : "保存后台工作配置失败");
    } finally {
      setIsSaving(false);
    }
  }

  if (config === null) {
    return (
      <Card>
        <CardHeader><CardTitle>后台工作</CardTitle></CardHeader>
        <CardContent>
          {errorMessage ? <div className="space-y-3"><p role="alert" className="text-sm text-destructive">{errorMessage}</p><Button onClick={() => setLoadAttempt(value => value + 1)} variant="outline">重新加载后台工作配置</Button></div>
            : <EmptyState>正在读取后台工作配置。</EmptyState>}
        </CardContent>
      </Card>
    );
  }

  const hasValidModel = model.trim().length > 0;
  const hasValidMaxTurns = isValidMaxTurns(maxTurns);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-4 space-y-0">
        <div className="space-y-1.5">
          <CardTitle>后台工作</CardTitle>
          <CardDescription>单独配置 Claude Code 接入方、凭据、模型和访问权限；与下方 LLM 职责配置互不继承。</CardDescription>
        </div>
        <Badge variant={config?.credential_configured ? "success" : "secondary"}>
          {config?.credential_configured ? "凭据已配置" : "尚未配置"}
        </Badge>
      </CardHeader>
      <CardContent>
        <form className="divide-y" onChange={() => { setSavedMessage(null); setRefreshWarningMessage(null); }} onSubmit={(event) => void handleSubmit(event)}>
          <EnvironmentField disabled={isSaving} label="ANTHROPIC_BASE_URL" value={environment.ANTHROPIC_BASE_URL} onChange={(value) => setEnvironment({ ...environment, ANTHROPIC_BASE_URL: value })} />
          <SettingRow description="按接入方文档选择 API Key 或 Auth Token；留空会保留同类型旧凭据。" layout="field" title="Claude Code 凭据">
            <div className="space-y-2">
              <Select aria-label="Claude Code 凭据类型" disabled={isSaving} onChange={(event) => setCredentialType(event.target.value as BackgroundAgentCredentialType)} value={credentialType}>
                <option value="api_key">ANTHROPIC_API_KEY</option>
                <option value="auth_token">ANTHROPIC_AUTH_TOKEN</option>
              </Select>
              <Input aria-label="Claude Code 凭据" autoComplete="off" disabled={isSaving} onChange={(event) => setCredentialValue(event.target.value)} placeholder={config?.credential_configured ? "已配置；留空保持不变" : "请输入凭据"} type="password" value={credentialValue} />
            </div>
          </SettingRow>
          <SettingRow description="填写此 Claude Code 接入方支持的模型名称，以接入方文档为准；不会沿用 Foreground 等 LLM 职责的模型。" layout="field" title="后台工作主模型">
            <Input
              aria-label="后台工作主模型"
              disabled={isSaving}
              onChange={(event) => setModel(event.target.value)}
              placeholder="请输入接入方的模型名称"
              value={model}
            />
          </SettingRow>
          <SettingRow description="留空时使用模型默认强度；较高强度通常会消耗更多输出 token。" layout="field" title="推理强度">
            <Select aria-label="后台工作推理强度" disabled={isSaving} onChange={(event) => setEffortLevel(event.target.value as BackgroundAgentEffortLevel | "")} value={effortLevel}>
              <option value="">模型默认（high）</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
              <option value="xhigh">xhigh</option>
              <option value="max">max</option>
            </Select>
          </SettingRow>
          <div className="py-4">
            <details>
              <summary className="cursor-pointer text-sm font-medium">高级模型映射</summary>
              <p className="mt-2 text-sm text-muted-foreground">只有 Claude Code 请求 Opus、Sonnet 或 Haiku 别名时才使用这些覆盖值。</p>
              <div className="mt-2 divide-y">
                {MODEL_ALIAS_FIELDS.map((key) => (
                  <EnvironmentField disabled={isSaving} key={key} label={key} value={environment[key]} onChange={(value) => setEnvironment({ ...environment, [key]: value })} />
                ))}
              </div>
            </details>
          </div>
          <details className="py-4">
            <summary className="cursor-pointer text-sm font-medium">高级运行参数</summary>
          <SettingRow description="每行一个 KEY=VALUE；只接受未被 NearMate 一等配置占用的 ANTHROPIC_ 或 CLAUDE_CODE_ 非密钥变量。" layout="field" title="高级环境变量">
            <Textarea aria-label="Claude Code 高级环境变量" disabled={isSaving} onChange={(event) => setAdvancedEnvironment(event.target.value)} rows={5} value={advancedEnvironment} />
          </SettingRow>
          <SettingRow description="达到上限时结束任务；填写 0 表示不限制轮数。" layout="field" title="最大执行轮数">
            <Input
              aria-label="后台工作最大轮数"
              disabled={isSaving}
              max={200}
              min={0}
              onChange={(event) => {
                const rawValue = event.target.value;
                setMaxTurns(rawValue === "" ? "" : Number(rawValue));
              }}
              step={1}
              type="number"
              value={maxTurns}
            />
          </SettingRow>
          </details>
          <SettingRow description={permissionMode === "bypassPermissions" ? "全权限模式不会为 Bash 或文件修改请求确认，请只在你信任当前任务时开启。" : "工作区内编辑自动允许，Bash 和其它敏感操作会向你请求确认。"} layout="field" title="权限模式">
            <Select aria-label="后台工作权限模式" disabled={isSaving} onChange={(event) => setPermissionMode(event.target.value as BackgroundAgentPermissionMode)} value={permissionMode}>
              <option value="acceptEdits">acceptEdits（默认）</option>
              <option value="bypassPermissions">bypassPermissions（全权限）</option>
            </Select>
          </SettingRow>
          {permissionMode === "bypassPermissions" ? (
            <div className="my-3 rounded-md border border-amber-400 bg-amber-50 px-3 py-2 text-sm font-medium text-amber-900" role="alert">
              全权限模式已开启：后台工作执行 Bash 等操作时不会再等待确认。
            </div>
          ) : null}
          {errorMessage !== null ? <p className="py-3 text-sm font-medium text-destructive" role="alert">{errorMessage}</p> : null}
          {refreshWarningMessage !== null ? <p className="py-3 text-sm font-medium text-warning" role="status">{refreshWarningMessage}</p> : null}
          {savedMessage !== null ? <p className="py-3 text-sm font-medium text-success" role="status">{savedMessage}</p> : null}
          <div className="flex justify-end pt-4"><Button disabled={isSaving || !hasValidModel || !hasValidMaxTurns} type="submit">{isSaving ? "保存中…" : "保存后台工作配置"}</Button></div>
        </form>
      </CardContent>
    </Card>
  );
}

function EnvironmentField({ disabled, label, value, onChange }: { disabled: boolean; label: KnownEnvironmentField; value: string; onChange: (value: string) => void }): JSX.Element {
  return (
    <SettingRow layout="field" title={label}>
      <Input aria-label={label} disabled={disabled} onChange={(event) => onChange(event.target.value)} value={value} />
    </SettingRow>
  );
}

function emptyEnvironmentDraft(): EnvironmentDraft {
  return Object.fromEntries(KNOWN_ENVIRONMENT_FIELDS.map((key) => [key, ""])) as EnvironmentDraft;
}

function parseAdvancedEnvironment(text: string): Record<string, string> {
  return Object.fromEntries(
    text.split(/\r?\n/).flatMap((line) => {
      const separatorIndex = line.indexOf("=");
      if (separatorIndex <= 0) {
        return [];
      }
      const key = line.slice(0, separatorIndex).trim().toUpperCase();
      const value = line.slice(separatorIndex + 1).trim();
      return key && value && !RESERVED_ADVANCED_ENVIRONMENT_KEYS.has(key) ? [[key, value]] : [];
    })
  );
}

/** 判断轮次输入是否能通过 Backend 的严格整数边界。 */
function isValidMaxTurns(value: number | ""): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 200;
}
