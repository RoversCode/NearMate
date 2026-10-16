import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  LlmConfig,
  LlmConfigUpdatePayload,
  LlmRoleName,
  LlmRoleStatus,
  SupportedLlmModelsResponse
} from "../contracts/llm";
import { fetchLlmConfig, fetchSupportedLlmModels, updateLlmConfig } from "./llm";

const FLASH_ID = "deepseek:deepseek-flash:chat";
const PRO_ID = "deepseek:deepseek-v4-pro:chat";
const QWEN_ID = "qwen:qwen3.6-plus:chat";
const QWEN_REPAIR_ID = "qwen:qwen3.6-flash:chat";

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * 构造后端已验证接入注册表的严格响应。
 *
 * Returns:
 *   包含两个厂商、四个接入规格和四个职责合同的响应。
 */
function createSupportedModelsPayload(): SupportedLlmModelsResponse {
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
        capabilities: ["image_input", "web_search", "web_extract"]
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
      createRoleContract("foreground", true, FLASH_ID, [FLASH_ID]),
      createRoleContract("summary", true, FLASH_ID, [FLASH_ID, PRO_ID]),
      createRoleContract("cognitive", true, QWEN_ID, [QWEN_ID]),
      createRoleContract("character_authoring", false, FLASH_ID, [FLASH_ID, PRO_ID])
    ]
  };
}

/**
 * 构造一个职责合同。
 *
 * Args:
 *   roleId: LLM 职责标识。
 *   required: 是否参与 Bootstrap 门禁。
 *   defaultIntegrationId: 默认接入规格编号。
 *   allowedIntegrationIds: 已验证的显式兼容边。
 *
 * Returns:
 *   可放入 supported-models 响应的职责合同。
 */
function createRoleContract(
  roleId: LlmRoleName,
  required: boolean,
  defaultIntegrationId: string,
  allowedIntegrationIds: string[]
): SupportedLlmModelsResponse["role_contracts"][number] {
  return {
    role_id: roleId,
    display_name: roleId,
    description: `${roleId} 测试合同`,
    required,
    default_integration_id: defaultIntegrationId,
    required_capabilities: roleId === "foreground" ? ["tool_calling", "image_input"] : [],
    allowed_integration_ids: allowedIntegrationIds
  };
}

/**
 * 构造 schema v2 配置快照。
 *
 * Returns:
 *   两家凭据齐全且四个职责都可运行的安全配置。
 */
function createConfigPayload(): LlmConfig {
  const roleBindings: LlmConfig["role_bindings"] = {
    foreground: FLASH_ID,
    summary: PRO_ID,
    cognitive: QWEN_ID,
    character_authoring: FLASH_ID
  };
  return {
    schema_version: 2,
    credentials: {
      deepseek: {
        base_url: "https://api.deepseek.com/v1",
        api_key_configured: true
      },
      qwen: {
        base_url: "https://workspace_123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        api_key_configured: true
      }
    },
    role_bindings: roleBindings,
    status: {
      is_ready: true,
      missing: [],
      errors: [],
      roles: {
        foreground: createRoleStatus(FLASH_ID, "deepseek"),
        summary: createRoleStatus(PRO_ID, "deepseek"),
        cognitive: createRoleStatus(QWEN_ID, "qwen"),
        character_authoring: createRoleStatus(FLASH_ID, "deepseek")
      }
    }
  };
}

/**
 * 构造一个可运行职责的状态。
 *
 * Args:
 *   integrationId: 当前绑定的接入规格编号。
 *   providerId: 当前绑定使用的厂商。
 *
 * Returns:
 *   不包含明文凭据的职责状态。
 */
function createRoleStatus(integrationId: string, providerId: string): LlmRoleStatus {
  return {
    enabled: true,
    is_ready: true,
    integration_id: integrationId,
    provider_id: providerId,
    missing: [],
    errors: []
  };
}

describe("LLM API service", () => {
  it("读取并保存严格 schema v2 配置", async () => {
    const supportedModelsPayload = createSupportedModelsPayload();
    const llmConfigPayload = createConfigPayload();
    const updatePayload: LlmConfigUpdatePayload = {
      schema_version: 2,
      credentials: {
        deepseek: {
          base_url: "https://api.deepseek.com/v1",
          api_key: ""
        },
        qwen: {
          base_url: "workspace_123",
          api_key: "qwen-key"
        }
      },
      role_bindings: {
        ...llmConfigPayload.role_bindings,
        foreground: FLASH_ID
      }
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(supportedModelsPayload)))
      .mockResolvedValueOnce(new Response(JSON.stringify(llmConfigPayload)))
      .mockResolvedValueOnce(new Response(JSON.stringify(llmConfigPayload), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    const models = await fetchSupportedLlmModels("http://127.0.0.1:8765");
    const config = await fetchLlmConfig("http://127.0.0.1:8765");
    const saved = await updateLlmConfig("http://127.0.0.1:8765", updatePayload);

    expect(models.role_contracts.find((contract) => contract.role_id === "cognitive")?.allowed_integration_ids).toEqual([
      QWEN_ID
    ]);
    expect(models.integrations.map((integration) => integration.integration_id)).toContain(QWEN_REPAIR_ID);
    expect(models.integrations.map((integration) => integration.integration_id)).toContain(FLASH_ID);
    expect(config.credentials.qwen.api_key_configured).toBe(true);
    expect(config.role_bindings.cognitive).toBe(QWEN_ID);
    expect(saved.status.roles.cognitive.is_ready).toBe(true);
    expect(fetchMock).toHaveBeenNthCalledWith(3, "http://127.0.0.1:8765/api/llm/config", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(updatePayload)
    });
  });

  it("把 Provider 连通性 400 detail 原样交给配置页面", async () => {
    const updatePayload: LlmConfigUpdatePayload = {
      schema_version: 2,
      credentials: {
        deepseek: {
          base_url: "https://api.deepseek.com/v1",
          api_key: "deepseek-key"
        },
        qwen: {
          base_url: "workspace_123",
          api_key: "qwen-key"
        }
      },
      role_bindings: createConfigPayload().role_bindings
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: "LLM Provider 连通性检查失败：Qwen [qwen]：authentication_failed"
          }),
          {
            status: 400,
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    await expect(updateLlmConfig("http://127.0.0.1:8765", updatePayload)).rejects.toThrow(
      "保存 LLM 配置失败：LLM Provider 连通性检查失败：Qwen [qwen]：authentication_failed"
    );
  });

  it.each([
    ["缺少职责键", { ...createConfigPayload().role_bindings, cognitive: undefined }],
    ["职责绑定类型错误", { ...createConfigPayload().role_bindings, cognitive: 123 }],
    ["包含未知职责", { ...createConfigPayload().role_bindings, router: FLASH_ID }]
  ])("拒绝%s的 LLM 配置响应", async (_caseName, roleBindings) => {
    const invalidPayload = {
      ...createConfigPayload(),
      role_bindings: roleBindings
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(invalidPayload))));

    await expect(fetchLlmConfig("http://127.0.0.1:8765")).rejects.toThrow(
      "读取 LLM 配置失败：响应格式不符合控制台契约"
    );
  });

  it("拒绝携带合同外字段的配置响应", async () => {
    const invalidPayload = {
      ...createConfigPayload(),
      unexpected_field: "unexpected-value"
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(invalidPayload))));

    await expect(fetchLlmConfig("http://127.0.0.1:8765")).rejects.toThrow(
      "读取 LLM 配置失败：响应格式不符合控制台契约"
    );
  });

  it("拒绝未进入 V1 的 API 接口形态", async () => {
    const stalePayload = createSupportedModelsPayload();
    const invalidPayload = {
      ...stalePayload,
      integrations: [
        ...stalePayload.integrations,
        {
          ...stalePayload.integrations[0],
          integration_id: "future:unverified-model:unsupported",
          api_surface: "unsupported"
        }
      ]
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(invalidPayload))));

    await expect(fetchSupportedLlmModels("http://127.0.0.1:8765")).rejects.toThrow(
      "读取支持模型失败：响应格式不符合控制台契约"
    );
  });
});
