import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { BootstrapInitializePayload, BootstrapStatus, QWeatherApiKeyPayload } from "../../api/contracts/bootstrap";
import { ServiceKeysPanel } from "./ServiceKeysPanel";

/**
 * 构造服务密钥面板所需的初始化状态。
 *
 * Args:
 *   voiceReady: 当前角色语音是否已就绪。
 *
 * Returns:
 *   LLM 已满足，仅语音就绪状态可变的四键契约载荷。
 */
function createStatus(voiceReady: boolean): BootstrapStatus {
  return {
    llm: { is_ready: true, missing: [], errors: [] },
    voice: {
      provider: "local_streamvox",
      is_ready: voiceReady,
      phase: voiceReady ? "ready" : "unconfigured",
      model_id: voiceReady ? "qwen3-tts-clone-0.6b-gguf" : null,
      role_name: voiceReady ? "huohua" : null,
      has_confirmed_voice: false
    },
    has_qweather_api_key: false,
    is_initialized: false
  };
}

type PanelOverrides = {
  status?: BootstrapStatus;
  mode?: "initial" | "settings";
  onBootstrapSubmit?: (payload: BootstrapInitializePayload) => Promise<boolean>;
  onQWeatherSubmit?: (payload: QWeatherApiKeyPayload) => Promise<boolean>;
};

/**
 * 以统一默认值渲染服务密钥面板。
 *
 * Args:
 *   overrides: 单个用例关心的状态与回调差异。
 *
 * Returns:
 *   Testing Library 渲染结果。
 */
function renderPanel(overrides: PanelOverrides = {}) {
  return render(
    <ServiceKeysPanel
      title="首次初始化"
      description="测试面板"
      status={overrides.status ?? createStatus(true)}
      mode={overrides.mode ?? "initial"}
      isSaving={false}
      errorMessage={null}
      onBootstrapSubmit={overrides.onBootstrapSubmit ?? vi.fn(async () => true)}
      onQWeatherSubmit={overrides.onQWeatherSubmit}
    />
  );
}

describe("ServiceKeysPanel", () => {
  it("完成初始化按钮跟随语音就绪状态启停", () => {
    const view = renderPanel({ status: createStatus(false) });
    expect((screen.getByRole("button", { name: "完成初始化" }) as HTMLButtonElement).disabled).toBe(true);

    view.rerender(
      <ServiceKeysPanel
        title="首次初始化"
        description="测试面板"
        status={createStatus(true)}
        mode="initial"
        isSaving={false}
        errorMessage={null}
        onBootstrapSubmit={vi.fn(async () => true)}
      />
    );
    expect((screen.getByRole("button", { name: "完成初始化" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("初始化提交成功后清空和风天气输入框", async () => {
    const onBootstrapSubmit = vi.fn(async () => true);
    renderPanel({ onBootstrapSubmit });

    fireEvent.change(screen.getByLabelText("和风天气 API Host"), { target: { value: " abc1234xyz.def.qweatherapi.com " } });
    fireEvent.change(screen.getByLabelText("和风天气 API Key"), { target: { value: " qweather-key " } });
    fireEvent.click(screen.getByRole("button", { name: "完成初始化" }));

    await waitFor(() => expect(onBootstrapSubmit).toHaveBeenCalledWith({
      qweather_api_key: "qweather-key",
      qweather_api_host: "abc1234xyz.def.qweatherapi.com"
    }));
    await waitFor(() => {
      expect((screen.getByLabelText("和风天气 API Host") as HTMLInputElement).value).toBe("");
      expect((screen.getByLabelText("和风天气 API Key") as HTMLInputElement).value).toBe("");
    });
  });

  it("初始化提交失败时保留用户已输入的密钥", async () => {
    const onBootstrapSubmit = vi.fn(async () => false);
    renderPanel({ onBootstrapSubmit });

    fireEvent.change(screen.getByLabelText("和风天气 API Host"), { target: { value: "abc1234xyz.def.qweatherapi.com" } });
    fireEvent.change(screen.getByLabelText("和风天气 API Key"), { target: { value: "qweather-key" } });
    fireEvent.click(screen.getByRole("button", { name: "完成初始化" }));

    await waitFor(() => expect(onBootstrapSubmit).toHaveBeenCalledTimes(1));
    expect((screen.getByLabelText("和风天气 API Host") as HTMLInputElement).value).toBe("abc1234xyz.def.qweatherapi.com");
    expect((screen.getByLabelText("和风天气 API Key") as HTMLInputElement).value).toBe("qweather-key");
  });

  it("和风天气单独保存失败时保留输入，成功后清空", async () => {
    const onQWeatherSubmit = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true);
    renderPanel({ mode: "settings", onQWeatherSubmit });

    fireEvent.change(screen.getByLabelText("和风天气 API Host"), { target: { value: "abc1234xyz.def.qweatherapi.com" } });
    fireEvent.change(screen.getByLabelText("和风天气 API Key"), { target: { value: "qweather-key" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));

    await waitFor(() => expect(onQWeatherSubmit).toHaveBeenCalledTimes(1));
    // 第一次保存失败，输入内容必须保留，避免用户重新填写。
    expect((screen.getByLabelText("和风天气 API Key") as HTMLInputElement).value).toBe("qweather-key");

    fireEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    await waitFor(() => expect(onQWeatherSubmit).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      expect((screen.getByLabelText("和风天气 API Host") as HTMLInputElement).value).toBe("");
      expect((screen.getByLabelText("和风天气 API Key") as HTMLInputElement).value).toBe("");
    });
  });
});
