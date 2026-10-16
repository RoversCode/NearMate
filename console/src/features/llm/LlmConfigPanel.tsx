import { type FormEvent, useId, useLayoutEffect, useRef, useState } from "react";

import type {
  LlmConfig,
  LlmConfigUpdatePayload,
  LlmIntegrationSpec,
  LlmProviderSpec,
  LlmRoleContract,
  LlmRoleName,
  SupportedLlmModelsResponse
} from "../../api/contracts/llm";
import { EmptyState } from "@/components/layout/EmptyState";
import { SettingRow } from "@/components/layout/SettingRow";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Switch } from "@/components/ui/switch";
import { NEARMATE_HELP_LINKS } from "@/brand/helpLinks";

const QWEN_BASE_URL_PATTERN =
  /^https:\/\/([A-Za-z0-9_-]+)\.cn-beijing\.maas\.aliyuncs\.com\/compatible-mode\/v1\/?$/;

type LlmConfigPanelProps = {
  title: string;
  description: string;
  config: LlmConfig | null;
  supportedModels: SupportedLlmModelsResponse | null;
  isSaving: boolean;
  errorMessage: string | null;
  onSubmit: (payload: LlmConfigUpdatePayload) => Promise<boolean>;
  mode?: "settings" | "onboarding";
  submitLabel?: string;
  onboardingProvider?: "deepseek" | "qwen";
  onNext?: () => void;
  onBack?: () => void;
};

type CredentialDraft = Record<string, { base_url: string; api_key: string }>;
type RoleBindingDraft = Record<LlmRoleName, string | null>;

/**
 * 渲染厂商凭据和四个显式 LLM 职责绑定。
 *
 * Args:
 *   title: 当前页面语境下的面板标题。
 *   description: 当前页面语境下的简短说明。
 *   config: 后端返回的不含明文 Key 的 schema v2 配置。
 *   supportedModels: 已验证厂商、接入规格和职责合同。
 *   isSaving: 是否正在探测并保存配置。
 *   errorMessage: 最近一次保存失败原因。
 *   onSubmit: 提交完整 schema v2 草稿的回调。
 *   mode: 首次初始化显示获取指引并收起模型选择，默认保留设置页布局。
 *   submitLabel: 空闲时的提交按钮文案，未指定时使用“保存 LLM 配置”。
 *   onboardingProvider: 首次引导当前展示的厂商；省略时保留完整表单。
 *   onNext: DeepSeek 输入满足本地校验后进入 Qwen，不提交配置。
 *   onBack: 从 Qwen 返回 DeepSeek，面板继续持有两家草稿。
 *
 * Returns:
 *   可复用于 Bootstrap 和设置页的 LLM 配置表单。
 */
export function LlmConfigPanel({
  title,
  description,
  config,
  supportedModels,
  isSaving,
  errorMessage,
  onSubmit,
  mode = "settings",
  submitLabel = "保存 LLM 配置",
  onboardingProvider,
  onNext,
  onBack
}: LlmConfigPanelProps): JSX.Element {
  const [credentials, setCredentials] = useState<CredentialDraft>({});
  const [roleBindings, setRoleBindings] = useState<RoleBindingDraft>({
    foreground: null,
    summary: null,
    cognitive: null,
    character_authoring: null
  });
  const [isDraftDirty, setIsDraftDirty] = useState(false);
  const isDraftDirtyRef = useRef(false);
  const isOnboarding = mode === "onboarding";

  useLayoutEffect(() => {
    // 用户开始编辑后，迟到的初始化请求或状态刷新不能覆盖尚未保存的草稿。
    if (config === null || isDraftDirtyRef.current) {
      return;
    }
    setCredentials(
      Object.fromEntries(
        Object.entries(config.credentials).map(([providerId, credential]) => [
          providerId,
          {
            base_url:
              providerId === "qwen"
                ? formatQwenCredentialForDisplay(credential.base_url)
                : credential.base_url,
            api_key: ""
          }
        ])
      )
    );
    setRoleBindings(config.role_bindings);
  }, [config, isDraftDirty]);

  if (config === null || supportedModels === null) {
    return (
      <Card className={isOnboarding ? "border-0 bg-transparent shadow-none" : undefined}>
        <CardHeader className={isOnboarding ? "px-0 pb-4 pt-0" : undefined}>
          <CardTitle>{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent className={isOnboarding ? "p-0" : undefined}>
          <EmptyState>正在读取 LLM 接入规格和当前配置。</EmptyState>
        </CardContent>
      </Card>
    );
  }

  const currentConfig = config;
  const currentSupportedModels = supportedModels;
  const integrationById = new Map(
    currentSupportedModels.integrations.map((integration) => [integration.integration_id, integration])
  );
  // 必需职责的空值只表示后端还没有保存绑定；界面已经展示合同默认值，因此校验和提交也必须使用同一个有效值。
  const effectiveRoleBindings: RoleBindingDraft = { ...roleBindings };
  for (const contract of currentSupportedModels.role_contracts) {
    effectiveRoleBindings[contract.role_id] = getEffectiveRoleBinding(contract, roleBindings);
  }
  const selectedBindings = currentSupportedModels.role_contracts
    .map((contract) => effectiveRoleBindings[contract.role_id])
    .filter((integrationId): integrationId is string => integrationId !== null);
  const hasAllSelectedCredentials = selectedBindings.every((integrationId) => {
    const integration = integrationById.get(integrationId);
    // 退休接入规格仍然要原样交给 Backend 校验，不能因为前端注册表已经删除它就把保存入口锁死。
    if (integration === undefined) {
      return true;
    }
    return hasProviderCredential(
      integration.provider_id,
      credentials,
      currentConfig
    );
  });
  const unavailableRoleBindings = new Set(
    currentSupportedModels.role_contracts
      .filter((contract) => {
        const binding = effectiveRoleBindings[contract.role_id];
        return binding !== null &&
          (!integrationById.has(binding) || !contract.allowed_integration_ids.includes(binding));
      })
      .map((contract) => contract.role_id)
  );
  const allRequiredBindingsPresent = currentSupportedModels.role_contracts.every(
    (contract) => !contract.required || effectiveRoleBindings[contract.role_id] !== null
  );
  const isSubmitDisabled =
    isSaving || !hasAllSelectedCredentials || !allRequiredBindingsPresent;
  const showingDeepSeekStep = isOnboarding && onboardingProvider === "deepseek";
  const showingQwenStep = isOnboarding && onboardingProvider === "qwen";
  const visibleProviders = isOnboarding && onboardingProvider
    ? currentSupportedModels.providers.filter(provider => provider.provider_id === onboardingProvider)
    : currentSupportedModels.providers;
  const canContinueDeepSeek = hasProviderCredential("deepseek", credentials, currentConfig);

  /**
   * 标记表单已经被用户修改。
   *
   * Returns:
   *   无返回值；后续配置刷新会保留当前草稿。
   */
  function markDraftDirty(): void {
    isDraftDirtyRef.current = true;
    setIsDraftDirty(true);
  }

  /**
   * 第一家填好后进入第二家，最后一次提交完整 schema v2 草稿。
   *
   * Args:
   *   event: 浏览器表单提交事件。
   *
   * Returns:
   *   无返回值；第一家不保存，最终保存成功后允许父组件的新快照覆盖草稿。
   */
  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    // 第一家只收集输入；两家凭据必须在最后一起验证和保存，不能产生半份配置。
    if (showingDeepSeekStep) {
      if (!isSaving && canContinueDeepSeek) onNext?.();
      return;
    }
    if (isSubmitDisabled) {
      return;
    }
    const saved = await onSubmit({
      schema_version: 2,
      credentials: Object.fromEntries(
        currentSupportedModels.providers.map((provider) => {
          const credential = credentials[provider.provider_id] ?? {
            base_url: provider.default_base_url,
            api_key: ""
          };
          return [
            provider.provider_id,
            {
              base_url: formatCredentialForSubmit(
                provider.provider_id,
                credential.base_url
              ),
              api_key: credential.api_key.trim()
            }
          ];
        })
      ),
      role_bindings: effectiveRoleBindings
    });
    if (!saved) {
      return;
    }
    isDraftDirtyRef.current = false;
    setIsDraftDirty(false);
  }

  const roleBindingSection = (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">LLM 职责绑定</h3>
      {currentSupportedModels.role_contracts.map((contract) => (
        <RoleBindingRow
          contract={contract}
          disabled={isSaving}
          integrations={contract.allowed_integration_ids
            .map((integrationId) => integrationById.get(integrationId))
            .filter((integration): integration is LlmIntegrationSpec => integration !== undefined)}
          key={contract.role_id}
          onChange={(integrationId) => {
            markDraftDirty();
            setRoleBindings((current) => ({
              ...current,
              [contract.role_id]: integrationId
            }));
          }}
          status={currentConfig.status.roles[contract.role_id]}
          bindingIsUnavailable={unavailableRoleBindings.has(contract.role_id)}
          value={effectiveRoleBindings[contract.role_id]}
        />
      ))}
    </section>
  );

  return (
    <Card className={isOnboarding ? "border-0 bg-transparent shadow-none" : undefined}>
      <CardHeader className={isOnboarding ? "px-0 pb-4 pt-0" : undefined}>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className={isOnboarding ? "space-y-4 p-0" : "space-y-5"}>
        {isOnboarding ? (
          <div className="space-y-2 rounded-lg bg-muted/50 p-3 text-sm">
            <p className="font-medium">{showingDeepSeekStep ? "先连接 DeepSeek" : showingQwenStep ? "再连接 Qwen（阿里云百炼）" : "先准备两个模型服务账户"}</p>
            <p>{showingDeepSeekStep ? "DeepSeek 负责日常对话和对话记忆。先准备它的 API Key，下一步再填写 Qwen；此步不会联网验证或保存。" : showingQwenStep ? "Qwen 负责联网检索和深入理解任务。填好工作空间 ID 和 API Key 后，我们会一起验证并保存两家配置。" : "DeepSeek 负责日常对话和对话记忆；Qwen（阿里云百炼）负责联网检索和深入理解任务。当前版本需要两家的服务才能开始使用。"}</p>
            <p className="text-muted-foreground">API Key 是模型厂商给你的访问密钥。模型服务由厂商单独计费，请根据自己的使用量管理厂商账户和余额。</p>
            <p className="text-muted-foreground">输入只暂存在当前页面。刷新或关闭页面后，未保存的 API Key 需要重新填写；两家验证保存成功后才会保留配置。</p>
            {onboardingProvider ? <a className="inline-block text-primary underline underline-offset-4" href={NEARMATE_HELP_LINKS.firstRun[onboardingProvider]} rel="noopener noreferrer" target="_blank">查看本步完整教程</a> : null}
          </div>
        ) : null}
        {errorMessage !== null ? (
          <p className="text-sm font-medium text-destructive" role="alert">
            {isOnboarding ? "连接验证未通过：" : "LLM 配置保存失败："}{errorMessage}
          </p>
        ) : null}
        {currentConfig.status.is_ready ? (
          <p className="text-sm font-medium text-success">
            {isOnboarding ? "模型服务已配置。已保存的 API Key 不会显示，留空即可继续使用。" : "所有必需 LLM 职责均可运行。"}
          </p>
        ) : !isOnboarding ? (
          <p className="text-sm font-medium text-warning">
            LLM 配置未完成：
            {[...currentConfig.status.missing, ...currentConfig.status.errors].join("，")}
          </p>
        ) : null}

        <form className="space-y-5" onSubmit={(event) => void handleSubmit(event)}>
          <section>
            <h3 className="pb-2 text-sm font-semibold">{isOnboarding ? "连接模型服务" : "厂商凭据"}</h3>
            {visibleProviders.map((provider) => (
              <ProviderCredentialRow
                credentialConfigured={
                  currentConfig.credentials[provider.provider_id]?.api_key_configured ?? false
                }
                disabled={isSaving}
                isOnboarding={isOnboarding}
                key={provider.provider_id}
                onChange={(nextCredential) => {
                  markDraftDirty();
                  setCredentials((current) => ({
                    ...current,
                    [provider.provider_id]: nextCredential
                  }));
                }}
                provider={provider}
                value={
                  credentials[provider.provider_id] ?? {
                    base_url: provider.default_base_url,
                    api_key: ""
                  }
                }
              />
            ))}
          </section>

          {!showingDeepSeekStep ? <Separator /> : null}

          {!showingDeepSeekStep && unavailableRoleBindings.size > 0 ? (
            <p className="text-sm font-medium text-warning" role="alert">
              {isOnboarding
                ? "之前选择的部分模型已停用或不适合当前用途。请在下方展开的高级设置中，为标记的项目重新选择模型，再验证继续。"
                : "检测到已退休或不适用于当前职责的模型绑定。保存请求会交给 Backend 返回详细错误；请为标记的职责重新选择现役模型。"}
            </p>
          ) : null}

          {showingDeepSeekStep ? null : isOnboarding ? (
            <details className="rounded-lg border p-4" open={unavailableRoleBindings.size > 0 || undefined}>
              <summary className="cursor-pointer text-sm font-medium">高级：模型与职责绑定</summary>
              <p className="py-3 text-sm text-muted-foreground">首次使用已按系统推荐分配模型，通常无需调整。已有模型选择会保留；角色创作是可选功能。</p>
              {roleBindingSection}
            </details>
          ) : roleBindingSection}

          {!(showingDeepSeekStep ? canContinueDeepSeek : hasAllSelectedCredentials) ? (
            <p className="text-sm font-medium text-warning">
              {showingDeepSeekStep ? "请填写 DeepSeek API Key。已经保存过的 API Key 可以留空。" : showingQwenStep
                ? "请填写 Qwen 的工作空间 ID 和 API Key；若 DeepSeek 尚未填好，可返回上一步修改。已经保存过的 API Key 可以留空。" : isOnboarding
                ? "请填写两家的 API Key，以及 Qwen 的工作空间 ID（WorkspaceId）。已经保存过的 API Key 可以留空。"
                : "请补齐所有现役模型绑定所需的厂商端点和 API Key。"}
            </p>
          ) : null}

          {isOnboarding && !showingDeepSeekStep ? <div className="space-y-2 text-sm text-muted-foreground"><p>点击下方按钮后会联网验证，再保存配置。验证失败时，你填写的内容会保留。</p>
            {showingQwenStep ? <a className="text-primary underline underline-offset-4" href={NEARMATE_HELP_LINKS.firstRun.verifyAi} rel="noopener noreferrer" target="_blank">查看两家连接验证说明</a> : null}
          </div> : null}
          <div className="flex flex-wrap justify-between gap-3">
            {showingQwenStep ? <Button disabled={isSaving} onClick={onBack} type="button" variant="ghost">上一步：连接 DeepSeek</Button> : <span />}
            <Button disabled={showingDeepSeekStep ? isSaving || !canContinueDeepSeek : isSubmitDisabled} type="submit">
              {isSaving ? "连接并保存中" : showingDeepSeekStep ? "下一步：连接 Qwen" : submitLabel}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function hasProviderCredential(
  providerId: string,
  credentials: CredentialDraft,
  config: LlmConfig
): boolean {
  const draft = credentials[providerId];
  const saved = config.credentials[providerId];
  return (
    draft !== undefined &&
    draft.base_url.trim().length > 0 &&
    (draft.api_key.trim().length > 0 || saved?.api_key_configured === true)
  );
}

/**
 * 计算职责当前真正要使用的绑定编号。
 *
 * Args:
 *   contract: 后端声明的职责合同。
 *   bindings: 当前表单草稿中的原始绑定。
 *
 * Returns:
 *   已选择的绑定编号；必需职责尚未保存绑定时返回合同默认值，可选职责保持关闭。
 */
function getEffectiveRoleBinding(
  contract: LlmRoleContract,
  bindings: RoleBindingDraft
): string | null {
  const binding = bindings[contract.role_id];
  if (binding !== null) {
    return binding;
  }
  return contract.required ? contract.default_integration_id : null;
}

function formatQwenCredentialForDisplay(baseUrl: string): string {
  const trimmedBaseUrl = baseUrl.trim();
  if (!trimmedBaseUrl) {
    return "";
  }
  return extractQwenWorkspaceId(trimmedBaseUrl) ?? trimmedBaseUrl;
}

function formatCredentialForSubmit(providerId: string, value: string): string {
  const trimmedValue = value.trim();
  if (providerId !== "qwen") {
    return trimmedValue;
  }
  return extractQwenWorkspaceId(trimmedValue) ?? trimmedValue;
}

function extractQwenWorkspaceId(value: string): string | null {
  const matched = QWEN_BASE_URL_PATTERN.exec(value.trim());
  return matched?.[1] ?? null;
}

type ProviderCredentialRowProps = {
  provider: LlmProviderSpec;
  value: { base_url: string; api_key: string };
  credentialConfigured: boolean;
  disabled: boolean;
  isOnboarding: boolean;
  onChange: (value: { base_url: string; api_key: string }) => void;
};

/**
 * 渲染单个厂商的端点和 API Key 输入。
 *
 * Args:
 *   provider: 后端返回的静态厂商凭据规格。
 *   value: 当前输入草稿。
 *   credentialConfigured: 后端是否已有该厂商 API Key。
 *   disabled: 是否禁用输入。
 *   isOnboarding: 是否显示新用户获取步骤和可见字段标签。
 *   onChange: 输入变化回调。
 *
 * Returns:
 *   一行不嵌套卡片的厂商凭据表单。
 */
function ProviderCredentialRow({
  provider,
  value,
  credentialConfigured,
  disabled,
  isOnboarding,
  onChange
}: ProviderCredentialRowProps): JSX.Element {
  const inputId = useId();
  const usesWorkspaceId = provider.endpoint_normalizer === "qwen_workspace";
  const providerLabel = provider.provider_id === "qwen" ? "Qwen（阿里云百炼）" : provider.display_name;
  const accessInputLabel = usesWorkspaceId
    ? isOnboarding ? "Qwen 工作空间 ID（WorkspaceId）" : `${provider.provider_id} WorkspaceId`
    : isOnboarding ? `${provider.display_name} 服务地址` : `${provider.provider_id} Base URL`;
  const keyInputLabel = `${isOnboarding ? provider.display_name : provider.provider_id} API Key`;
  const qwenUrlParseWarning =
    usesWorkspaceId &&
    value.base_url.startsWith("http") &&
    extractQwenWorkspaceId(value.base_url) === null;

  return (
    <div className="grid gap-3 border-b py-4 last:border-b-0 sm:grid-cols-[minmax(12rem,1fr)_minmax(18rem,24rem)]">
      <div className="min-w-0 space-y-1">
        <div className="flex items-center gap-2">
          <h4 className="text-sm font-medium">{isOnboarding ? providerLabel : provider.display_name}</h4>
          <Badge variant={credentialConfigured ? "success" : "secondary"}>
            {credentialConfigured ? "已配置" : "未配置"}
          </Badge>
        </div>
        {isOnboarding && provider.provider_id === "deepseek" ? (
          <div className="space-y-2 text-sm text-muted-foreground">
            <p>用于与你对话，并整理对话中的记忆。</p>
            <details>
              <summary className="cursor-pointer font-medium text-primary underline underline-offset-4">没有 API Key？查看获取步骤</summary>
              <ol className="mt-2 list-decimal space-y-1 pl-5">
                <li><a className="text-primary underline underline-offset-4" href="https://platform.deepseek.com/" rel="noopener noreferrer" target="_blank">打开 DeepSeek 开放平台</a>，注册或登录。</li>
                <li>进入「API Keys」创建密钥，复制后填到对应输入框。</li>
                <li>检查账户余额，按厂商提示充值或开通服务。</li>
              </ol>
            </details>
          </div>
        ) : null}
        {isOnboarding && usesWorkspaceId ? (
          <div className="space-y-2 text-sm text-muted-foreground">
            <p>用于联网查资料和深入理解任务，需要密钥和工作空间 ID 两项信息。</p>
            <details>
              <summary className="cursor-pointer font-medium text-primary underline underline-offset-4">没有 API Key 或工作空间 ID？查看获取步骤</summary>
              <ol className="mt-2 list-decimal space-y-1 pl-5">
                <li><a className="text-primary underline underline-offset-4" href="https://bailian.console.aliyun.com/" rel="noopener noreferrer" target="_blank">打开阿里云百炼控制台</a>，按提示开通模型服务。</li>
                <li>在北京地域选择使用的工作空间，从工作空间管理复制 WorkspaceId。</li>
                <li>在「API-Key 管理」创建该空间可用的密钥，并检查服务额度。</li>
              </ol>
            </details>
          </div>
        ) : null}
        {qwenUrlParseWarning ? (
          <p className="text-sm font-medium text-warning">
            当前 Qwen 地址无法识别，请重新填写 WorkspaceId。
          </p>
        ) : null}
      </div>
      <div className="space-y-2">
        {isOnboarding ? <label className="block text-sm font-medium" htmlFor={`${inputId}-endpoint`}>{accessInputLabel}</label> : null}
        <Input
          aria-label={accessInputLabel}
          disabled={disabled}
          id={`${inputId}-endpoint`}
          onChange={(event) =>
            onChange({
              ...value,
              base_url: event.target.value
            })
          }
          placeholder={usesWorkspaceId ? "WorkspaceId" : "https://example.com/v1"}
          value={value.base_url}
        />
        {isOnboarding ? (
          <p className="text-xs text-muted-foreground">
            {usesWorkspaceId ? "必填。这里只填工作空间 ID，不是 API Key，也不是阿里云账号 ID。" : "已填好官方服务地址，通常不需要修改。"}
          </p>
        ) : null}
        {isOnboarding ? <label className="block pt-2 text-sm font-medium" htmlFor={`${inputId}-key`}>{keyInputLabel}</label> : null}
        <Input
          aria-label={keyInputLabel}
          autoComplete="off"
          disabled={disabled}
          id={`${inputId}-key`}
          onChange={(event) =>
            onChange({
              ...value,
              api_key: event.target.value
            })
          }
          placeholder={credentialConfigured ? "留空保留已保存密钥" : "API Key"}
          type="password"
          value={value.api_key}
        />
        {isOnboarding && credentialConfigured ? <p className="text-xs text-muted-foreground">已保存 API Key，留空会保留；填写新值才会替换。</p> : null}
      </div>
    </div>
  );
}

type RoleBindingRowProps = {
  contract: LlmRoleContract;
  value: string | null;
  integrations: LlmIntegrationSpec[];
  disabled: boolean;
  status: LlmConfig["status"]["roles"][LlmRoleName];
  bindingIsUnavailable: boolean;
  onChange: (integrationId: string | null) => void;
};

/**
 * 渲染单个职责的显式模型接入绑定。
 *
 * Args:
 *   contract: 后端声明的职责必需性和允许边。
 *   value: 当前绑定编号，空值表示可选职责关闭。
 *   integrations: 只来自 allowed_integration_ids 的候选。
 *   disabled: 是否禁用输入。
 *   status: 已提交配置对应的职责状态。
 *   onChange: 绑定或开关变化回调。
 *
 * Returns:
 *   必需职责选择器或带开关的可选职责选择器。
 */
function RoleBindingRow({
  contract,
  value,
  integrations,
  disabled,
  status,
  bindingIsUnavailable,
  onChange
}: RoleBindingRowProps): JSX.Element {
  const enabled = contract.required || value !== null;
  const selectedValue = value ?? contract.default_integration_id;
  return (
    <SettingRow
      description={contract.description}
      layout="field"
      title={contract.display_name}
    >
      <div className="flex min-w-0 flex-wrap items-center justify-end gap-3">
        <Badge variant={contract.required ? "secondary" : "outline"}>
          {contract.required ? "必需" : "可选"}
        </Badge>
        {!contract.required ? (
          <Switch
            aria-label={`启用 ${contract.role_id}`}
            checked={enabled}
            disabled={disabled}
            onCheckedChange={(checked) =>
              onChange(checked ? contract.default_integration_id : null)
            }
          />
        ) : null}
        <Select
          aria-label={`${contract.role_id} 模型`}
          className="min-w-56"
          disabled={disabled || !enabled}
          onChange={(event) => onChange(event.target.value)}
          value={selectedValue}
        >
          {bindingIsUnavailable ? (
            <option value={selectedValue}>当前绑定不可用，请重新选择</option>
          ) : null}
          {integrations.map((integration) => (
            <option
              key={integration.integration_id}
              value={integration.integration_id}
            >
              {integration.display_name}
            </option>
          ))}
        </Select>
        <Badge variant={bindingIsUnavailable ? "warning" : status.is_ready ? "success" : enabled ? "warning" : "secondary"}>
          {bindingIsUnavailable ? "需重新选择" : enabled ? (status.is_ready ? "可运行" : "待配置") : "已关闭"}
        </Badge>
      </div>
      {bindingIsUnavailable ? (
        <p className="basis-full text-right text-sm font-medium text-warning">
          当前绑定不可用，请重新选择现役模型。
        </p>
      ) : null}
    </SettingRow>
  );
}
