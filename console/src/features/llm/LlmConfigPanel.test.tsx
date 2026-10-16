import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  LlmConfig,
  LlmConfigUpdatePayload,
  LlmRoleName,
  LlmRoleStatus,
  SupportedLlmModelsResponse
} from "../../api/contracts/llm";
import { LlmConfigPanel } from "./LlmConfigPanel";

const FLASH_ID = "deepseek:deepseek-flash:chat";
const PRO_ID = "deepseek:deepseek-v4-pro:chat";
const QWEN_ID = "qwen:qwen3.6-plus:chat";
const QWEN_REPAIR_ID = "qwen:qwen3.6-flash:chat";

/**
 * 构造面板使用的已验证接入注册表。
 *
 * Returns:
 *   两个厂商、四个接入规格和四个职责合同。
 */
function createSupportedModels(): SupportedLlmModelsResponse {
  return {
    providers: [
      {
        provider_id: "deepseek",
        display_name: "DeepSeek",
        default_base_url: "https://api.deepseek.com/v1",
        default_base_url_allowed: true,
        endpoint_normalizer: "standard_url"
      },
      {
        provider_id: "qwen",
        display_name: "Qwen",
        default_base_url: "",
        default_base_url_allowed: false,
        endpoint_normalizer: "qwen_workspace"
      }
    ],
    integrations: [
      {
        integration_id: FLASH_ID,
        provider_id: "deepseek",
        model: "deepseek-flash",
        display_name: "DeepSeek V4.1 Flash",
        api_surface: "chat_completions",
        capabilities: ["tool_calling", "json_output", "image_input"]
      },
      {
        integration_id: PRO_ID,
        provider_id: "deepseek",
        model: "deepseek-v4-pro",
        display_name: "DeepSeek V4 Pro",
        api_surface: "chat_completions",
        capabilities: ["tool_calling", "json_output"]
      },
      {
        integration_id: QWEN_ID,
        provider_id: "qwen",
        model: "qwen3.6-plus",
        display_name: "Qwen 3.6 Plus",
        api_surface: "chat_completions",
        capabilities: ["image_input", "json_output", "web_search", "web_extract"]
      },
      {
        integration_id: QWEN_REPAIR_ID,
        provider_id: "qwen",
        model: "qwen3.6-flash",
        display_name: "Qwen 3.6 Flash",
        api_surface: "chat_completions",
        capabilities: ["json_output"]
      }
    ],
    role_contracts: [
      createRoleContract("foreground", "Foreground", true, FLASH_ID, [FLASH_ID]),
      createRoleContract("summary", "Summary", true, FLASH_ID, [FLASH_ID, PRO_ID]),
      createRoleContract("cognitive", "Cognitive", true, QWEN_ID, [QWEN_ID]),
      createRoleContract("character_authoring", "Character Authoring", false, FLASH_ID, [
        FLASH_ID,
        PRO_ID
      ])
    ]
  };
}

/**
 * 构造一个职责合同。
 *
 * Args:
 *   roleId: LLM 职责标识。
 *   displayName: 面板展示名称。
 *   required: 是否参与 Bootstrap 门禁。
 *   defaultIntegrationId: 默认接入规格编号。
 *   allowedIntegrationIds: 已验证的显式兼容边。
 *
 * Returns:
 *   面板可以直接消费的职责合同。
 */
function createRoleContract(
  roleId: LlmRoleName,
  displayName: string,
  required: boolean,
  defaultIntegrationId: string,
  allowedIntegrationIds: string[]
): SupportedLlmModelsResponse["role_contracts"][number] {
  return {
    role_id: roleId,
    display_name: displayName,
    description: `${displayName} 测试说明`,
    required,
    default_integration_id: defaultIntegrationId,
    required_capabilities: roleId === "foreground" ? ["tool_calling", "image_input"] : [],
    allowed_integration_ids: allowedIntegrationIds
  };
}

/**
 * 构造面板使用的 schema v2 配置。
 *
 * Args:
 *   qwenBaseUrl: 后端保存的 Qwen 完整地址。
 *   qwenApiKeyConfigured: 后端是否已有 Qwen API Key。
 *   characterAuthoringBinding: 可选角色创作职责的绑定。
 *
 * Returns:
 *   包含四个逐职责状态的安全配置快照。
 */
function createLlmConfig(
  qwenBaseUrl: string,
  qwenApiKeyConfigured = true,
  characterAuthoringBinding: string | null = FLASH_ID
): LlmConfig {
  const cognitiveStatus = qwenApiKeyConfigured
    ? createRoleStatus(QWEN_ID, "qwen")
    : createRoleStatus(QWEN_ID, "qwen", false, ["qwen.api_key"]);
  const characterAuthoringStatus =
    characterAuthoringBinding === null
      ? {
          enabled: false,
          is_ready: true,
          integration_id: null,
          provider_id: null,
          missing: [],
          errors: []
        }
      : createRoleStatus(characterAuthoringBinding, "deepseek");
  return {
    schema_version: 2,
    credentials: {
      deepseek: {
        base_url: "https://api.deepseek.com/v1",
        api_key_configured: true
      },
      qwen: {
        base_url: qwenBaseUrl,
        api_key_configured: qwenApiKeyConfigured
      }
    },
    role_bindings: {
      foreground: FLASH_ID,
      summary: PRO_ID,
      cognitive: QWEN_ID,
      character_authoring: characterAuthoringBinding
    },
    status: {
      is_ready: qwenApiKeyConfigured,
      missing: qwenApiKeyConfigured ? [] : ["qwen.api_key"],
      errors: [],
      roles: {
        foreground: createRoleStatus(FLASH_ID, "deepseek"),
        summary: createRoleStatus(PRO_ID, "deepseek"),
        cognitive: cognitiveStatus,
        character_authoring: characterAuthoringStatus
      }
    }
  };
}

/**
 * 构造一个职责状态。
 *
 * Args:
 *   integrationId: 当前接入规格编号。
 *   providerId: 当前厂商标识。
 *   isReady: 当前职责是否可运行。
 *   missing: 当前职责缺失的凭据字段。
 *
 * Returns:
 *   不包含明文凭据的职责状态。
 */
function createRoleStatus(
  integrationId: string,
  providerId: string,
  isReady = true,
  missing: string[] = []
): LlmRoleStatus {
  return {
    enabled: true,
    is_ready: isReady,
    integration_id: integrationId,
    provider_id: providerId,
    missing,
    errors: []
  };
}

describe("LlmConfigPanel", () => {
  it("两家引导连续显示，第一步不保存，统一验证失败后仍可返回修改两家的草稿", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(false);
    const config = createLlmConfig("");
    config.credentials.deepseek.api_key_configured = false;
    config.credentials.qwen.api_key_configured = false;

    /** 用同一个面板跨厂商切屏，避免测试替身替它保管密钥草稿。 */
    function ProviderJourney(): JSX.Element {
      const [provider, setProvider] = useState<"deepseek" | "qwen">("deepseek");
      return <LlmConfigPanel config={config} description="测试说明" errorMessage={null}
        isSaving={false} mode="onboarding" onboardingProvider={provider}
        onNext={() => setProvider("qwen")} onBack={() => setProvider("deepseek")}
        onSubmit={onSubmit} supportedModels={createSupportedModels()} title="连接模型服务"
        submitLabel="验证两家连接并继续" />;
    }

    render(<ProviderJourney />);
    expect(screen.queryByLabelText("Qwen API Key")).toBeNull();
    expect(screen.getByRole("link", { name: "查看本步完整教程" }).getAttribute("href")).toContain("#deepseek");
    expect(screen.getByText(/刷新或关闭页面后，未保存的 API Key 需要重新填写/)).toBeTruthy();
    const next = screen.getByRole("button", { name: "下一步：连接 Qwen" }) as HTMLButtonElement;
    expect(next.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("DeepSeek API Key"), { target: { value: " deepseek-draft " } });
    fireEvent.click(next);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("DeepSeek API Key")).toBeNull();
    expect(screen.getByRole("link", { name: "查看本步完整教程" }).getAttribute("href")).toContain("#qwen");
    fireEvent.change(screen.getByLabelText("Qwen 工作空间 ID（WorkspaceId）"), { target: { value: " workspace_next " } });
    fireEvent.change(screen.getByLabelText("Qwen API Key"), { target: { value: " qwen-draft " } });
    fireEvent.click(screen.getByRole("button", { name: "验证两家连接并继续" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ credentials: {
      deepseek: { base_url: "https://api.deepseek.com/v1", api_key: "deepseek-draft" },
      qwen: { base_url: "workspace_next", api_key: "qwen-draft" }
    } }));
    fireEvent.click(screen.getByRole("button", { name: "上一步：连接 DeepSeek" }));
    expect((screen.getByLabelText("DeepSeek API Key") as HTMLInputElement).value).toBe(" deepseek-draft ");
    fireEvent.click(screen.getByRole("button", { name: "下一步：连接 Qwen" }));
    expect((screen.getByLabelText("Qwen API Key") as HTMLInputElement).value).toBe(" qwen-draft ");
    expect((screen.getByLabelText("Qwen 工作空间 ID（WorkspaceId）") as HTMLInputElement).value).toBe(" workspace_next ");
  });

  it("初始化说明两家服务的用途、独立计费和获取方式，并显示真正的字段标签", () => {
    renderPanel(createLlmConfig("workspace_123"), undefined, "onboarding");

    expect(screen.getByText(/DeepSeek 负责日常对话和对话记忆/)).toBeTruthy();
    expect(screen.getByText(/API Key 是模型厂商给你的访问密钥/).textContent).toContain("厂商单独计费");
    for (const label of ["没有 API Key？查看获取步骤", "没有 API Key 或工作空间 ID？查看获取步骤"]) {
      const help = screen.getByText(label);
      expect((help.parentElement as HTMLDetailsElement).open).toBe(false);
      fireEvent.click(help);
      expect((help.parentElement as HTMLDetailsElement).open).toBe(true);
    }
    expect(screen.getByRole("link", { name: "打开 DeepSeek 开放平台" }).getAttribute("href")).toBe("https://platform.deepseek.com/");
    expect(screen.getByRole("link", { name: "打开阿里云百炼控制台" }).getAttribute("href")).toBe("https://bailian.console.aliyun.com/");
    expect(screen.getByText(/在北京地域选择使用的工作空间/)).toBeTruthy();
    for (const name of ["DeepSeek 服务地址", "DeepSeek API Key", "Qwen 工作空间 ID（WorkspaceId）", "Qwen API Key"]) {
      const input = screen.getByLabelText(name) as HTMLInputElement;
      expect(Array.from(input.labels ?? []).map((label) => label.textContent)).toContain(name);
    }
    expect(screen.getAllByText("已保存 API Key，留空会保留；填写新值才会替换。")).toHaveLength(2);
    expect((screen.getByText("高级：模型与职责绑定").parentElement as HTMLDetailsElement).open).toBe(false);
  });

  it("初始化必须填写 Qwen 工作空间 ID，即使 API Key 已经保存", () => {
    renderPanel(createLlmConfig(""), undefined, "onboarding");

    const saveButton = screen.getByRole("button", { name: "验证并继续" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Qwen 工作空间 ID（WorkspaceId）"), { target: { value: "workspace_new" } });
    expect(saveButton.disabled).toBe(false);
  });

  it("初始化按后端合同默认分配未配置职责，并保留已保存的密钥", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    const config = createLlmConfig("workspace_123");
    config.role_bindings = { foreground: null, summary: null, cognitive: null, character_authoring: null };
    const supportedModels = createSupportedModels();
    supportedModels.role_contracts.find((contract) => contract.role_id === "summary")!.default_integration_id = PRO_ID;
    render(
      <LlmConfigPanel
        config={config}
        description="测试说明"
        errorMessage={null}
        isSaving={false}
        mode="onboarding"
        onSubmit={onSubmit}
        submitLabel="验证并继续"
        supportedModels={supportedModels}
        title="连接模型服务"
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({
      schema_version: 2,
      credentials: {
        deepseek: { base_url: "https://api.deepseek.com/v1", api_key: "" },
        qwen: { base_url: "workspace_123", api_key: "" }
      },
      role_bindings: { foreground: FLASH_ID, summary: PRO_ID, cognitive: QWEN_ID, character_authoring: null }
    }));
  });

  it("初始化遇到退休绑定时展开高级设置并保留原值，直到用户重新选择", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(false);
    const config = createLlmConfig("workspace_123");
    const retiredId = "deepseek:deepseek-v4-flash-vision-exp:chat";
    config.role_bindings.foreground = retiredId;
    renderPanel(config, onSubmit, "onboarding");

    expect((screen.getByText("高级：模型与职责绑定").parentElement as HTMLDetailsElement).open).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("之前选择的部分模型已停用");
    expect((screen.getByLabelText("foreground 模型") as HTMLSelectElement).value).toBe(retiredId);
    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() => expect(onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({
      role_bindings: expect.objectContaining({ foreground: retiredId })
    })));

    fireEvent.change(screen.getByLabelText("foreground 模型"), { target: { value: FLASH_ID } });
    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() => expect(onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({
      role_bindings: expect.objectContaining({ foreground: FLASH_ID })
    })));
  });

  it("初始化验证失败或返回前一步时保留已输入草稿，后台刷新不能覆盖", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(false);
    const supportedModels = createSupportedModels();
    const renderWizardStep = (hidden: boolean, errorMessage: string | null) => (
      <div hidden={hidden}>
        <LlmConfigPanel
          config={createLlmConfig("workspace_123")}
          description="测试说明"
          errorMessage={errorMessage}
          isSaving={false}
          mode="onboarding"
          onSubmit={onSubmit}
          submitLabel="验证并继续"
          supportedModels={supportedModels}
          title="连接模型服务"
        />
      </div>
    );
    const { rerender } = render(renderWizardStep(false, null));
    fireEvent.change(screen.getByLabelText("Qwen API Key"), { target: { value: "new-secret" } });
    fireEvent.change(screen.getByLabelText("Qwen 工作空间 ID（WorkspaceId）"), { target: { value: "new-workspace" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    rerender(renderWizardStep(false, "Qwen 密钥无效"));
    expect(screen.getByRole("alert").textContent).toBe("连接验证未通过：Qwen 密钥无效");

    rerender(renderWizardStep(true, null));
    rerender(renderWizardStep(false, null));

    expect((screen.getByLabelText("Qwen API Key") as HTMLInputElement).value).toBe("new-secret");
    expect((screen.getByLabelText("Qwen 工作空间 ID（WorkspaceId）") as HTMLInputElement).value).toBe("new-workspace");
  });

  it("展示保存接口返回的 Provider 连通性错误", () => {
    render(
      <LlmConfigPanel
        config={createLlmConfig("workspace_123")}
        description="测试说明"
        errorMessage="保存 LLM 配置失败：LLM Provider 连通性检查失败：Qwen [qwen]：authentication_failed"
        isSaving={false}
        onSubmit={vi.fn()}
        supportedModels={createSupportedModels()}
        title="LLM 配置"
      />
    );

    expect(
      screen.getByText(
        "LLM 配置保存失败：保存 LLM 配置失败：LLM Provider 连通性检查失败：Qwen [qwen]：authentication_failed"
      )
    ).toBeTruthy();
  });

  it("Qwen 凭据只展示 WorkspaceId，并以 schema v2 保存", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    render(
      <LlmConfigPanel
        config={createLlmConfig(
          "https://workspace_123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
          false
        )}
        description="测试说明"
        errorMessage={null}
        isSaving={false}
        onSubmit={onSubmit}
        supportedModels={createSupportedModels()}
        title="LLM 配置"
      />
    );

    const workspaceInput = screen.getByLabelText("qwen WorkspaceId") as HTMLInputElement;
    expect(workspaceInput.value).toBe("workspace_123");
    fireEvent.change(workspaceInput, { target: { value: " workspace_next " } });
    fireEvent.change(screen.getByLabelText("qwen API Key"), { target: { value: " qwen-key " } });
    fireEvent.click(screen.getByRole("button", { name: "保存 LLM 配置" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith({
        schema_version: 2,
        credentials: {
          deepseek: {
            base_url: "https://api.deepseek.com/v1",
            api_key: ""
          },
          qwen: {
            base_url: "workspace_next",
            api_key: "qwen-key"
          }
        },
        role_bindings: {
          foreground: FLASH_ID,
          summary: PRO_ID,
          cognitive: QWEN_ID,
          character_authoring: FLASH_ID
        }
      });
    });
  });

  it("Qwen 完整 URL 反解析失败时保留原值并提示异常", () => {
    renderPanel(createLlmConfig("https://legacy-qwen.example.com/compatible-mode/v1"));

    const workspaceInput = screen.getByLabelText("qwen WorkspaceId") as HTMLInputElement;
    expect(workspaceInput.value).toBe("https://legacy-qwen.example.com/compatible-mode/v1");
    expect(screen.getByText("当前 Qwen 地址无法识别，请重新填写 WorkspaceId。")).toBeTruthy();
  });

  it("用户开始编辑后，迟到的配置刷新不能覆盖当前草稿", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    const supportedModels = createSupportedModels();
    const { rerender } = render(
      <LlmConfigPanel
        config={createLlmConfig("https://workspace_initial.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")}
        description="测试说明"
        errorMessage={null}
        isSaving={false}
        onSubmit={onSubmit}
        supportedModels={supportedModels}
        title="LLM 配置"
      />
    );
    const workspaceInput = screen.getByLabelText("qwen WorkspaceId") as HTMLInputElement;
    const apiKeyInput = screen.getByLabelText("qwen API Key") as HTMLInputElement;

    fireEvent.change(workspaceInput, { target: { value: "workspace_user_draft" } });
    fireEvent.change(apiKeyInput, { target: { value: "user-secret" } });
    rerender(
      <LlmConfigPanel
        config={createLlmConfig("https://workspace_late.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")}
        description="测试说明"
        errorMessage={null}
        isSaving={false}
        onSubmit={onSubmit}
        supportedModels={supportedModels}
        title="LLM 配置"
      />
    );

    await waitFor(() => {
      expect(workspaceInput.value).toBe("workspace_user_draft");
      expect(apiKeyInput.value).toBe("user-secret");
    });
  });

  it("四个职责分别展示且候选只来自显式兼容矩阵", () => {
    renderPanel(createLlmConfig("workspace_123"));

    const foreground = screen.getByLabelText("foreground 模型") as HTMLSelectElement;
    const summary = screen.getByLabelText("summary 模型") as HTMLSelectElement;
    const cognitive = screen.getByLabelText("cognitive 模型") as HTMLSelectElement;
    const characterAuthoring = screen.getByLabelText("character_authoring 模型") as HTMLSelectElement;
    expect(Array.from(foreground.options).map((option) => option.value)).toEqual([FLASH_ID]);
    expect(Array.from(summary.options).map((option) => option.value)).toEqual([
      FLASH_ID,
      PRO_ID
    ]);
    expect(Array.from(cognitive.options).map((option) => option.value)).toEqual([QWEN_ID]);
    expect(screen.queryByText("Qwen 3.6 Flash")).toBeNull();
    expect(Array.from(characterAuthoring.options).map((option) => option.value)).toEqual([
      FLASH_ID,
      PRO_ID
    ]);
    expect(screen.getAllByText("必需")).toHaveLength(3);
    expect(screen.getByText("可选")).toBeTruthy();
  });

  it("默认 Foreground 原生视觉绑定按 schema v2 保存", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    renderPanel(createLlmConfig("workspace_123"), onSubmit);

    fireEvent.change(screen.getByLabelText("foreground 模型"), {
      target: { value: FLASH_ID }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存 LLM 配置" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          role_bindings: expect.objectContaining({
            foreground: FLASH_ID
          })
        })
      );
    });
  });

  it("必需职责为空时使用合同默认绑定并允许保存", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    const config = createLlmConfig("workspace_123");
    config.role_bindings = {
      foreground: null,
      summary: null,
      cognitive: null,
      character_authoring: null
    };

    renderPanel(config, onSubmit);

    const saveButton = screen.getByRole("button", { name: "保存 LLM 配置" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(false);
    expect((screen.getByLabelText("foreground 模型") as HTMLSelectElement).value).toBe(FLASH_ID);
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          role_bindings: {
            foreground: FLASH_ID,
            summary: FLASH_ID,
            cognitive: QWEN_ID,
            character_authoring: null
          }
        })
      );
    });
  });

  it("Cognitive 缺少 Qwen 凭据时阻止保存", () => {
    renderPanel(createLlmConfig("workspace_123", false));

    expect(screen.getByText("LLM 配置未完成：qwen.api_key")).toBeTruthy();
    expect(screen.getByText("请补齐所有现役模型绑定所需的厂商端点和 API Key。")).toBeTruthy();
    expect((screen.getByRole("button", { name: "保存 LLM 配置" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("退休模型绑定不会因为前端找不到接入规格而永久阻止保存", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    const config = createLlmConfig("workspace_123");
    config.role_bindings = {
      foreground: "deepseek:deepseek-v4-flash-vision-exp:chat",
      summary: "deepseek:deepseek-v4-flash:chat",
      cognitive: QWEN_ID,
      character_authoring: "deepseek:deepseek-v4-flash:chat"
    };

    renderPanel(config, onSubmit);

    expect(screen.getAllByText("当前绑定不可用，请重新选择现役模型。")).toHaveLength(3);
    const saveButton = screen.getByRole("button", { name: "保存 LLM 配置" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(false);
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          role_bindings: config.role_bindings
        })
      );
    });
  });

  it("Character Authoring 可以关闭并以 null 保存", async () => {
    const onSubmit = vi.fn<(_: LlmConfigUpdatePayload) => Promise<boolean>>().mockResolvedValue(true);
    renderPanel(createLlmConfig("workspace_123"), onSubmit);

    fireEvent.click(screen.getByRole("switch", { name: "启用 character_authoring" }));
    expect((screen.getByLabelText("character_authoring 模型") as HTMLSelectElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存 LLM 配置" }));

    await waitFor(() => {
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          role_bindings: expect.objectContaining({
            character_authoring: null
          })
        })
      );
    });
  });
});

/**
 * 使用统一属性渲染 LLM 配置面板。
 *
 * Args:
 *   config: 本次测试使用的配置快照。
 *   onSubmit: 可选保存回调。
 *   mode: 本次测试使用设置页还是首次初始化布局。
 *
 * Returns:
 *   Testing Library 的渲染结果。
 */
function renderPanel(
  config: LlmConfig,
  onSubmit: (payload: LlmConfigUpdatePayload) => Promise<boolean> = vi.fn().mockResolvedValue(true),
  mode: "settings" | "onboarding" = "settings"
): ReturnType<typeof render> {
  return render(
    <LlmConfigPanel
      config={config}
      description="测试说明"
      errorMessage={null}
      isSaving={false}
      mode={mode}
      onSubmit={onSubmit}
      submitLabel={mode === "onboarding" ? "验证并继续" : undefined}
      supportedModels={createSupportedModels()}
      title="LLM 配置"
    />
  );
}
