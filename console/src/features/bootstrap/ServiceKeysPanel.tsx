import { type FormEvent, useState } from "react";

import type { BootstrapInitializePayload, BootstrapStatus, QWeatherApiKeyPayload } from "../../api/contracts/bootstrap";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { SettingRow } from "@/components/layout/SettingRow";

type ServiceKeysPanelProps = {
  title: string;
  description: string;
  status: BootstrapStatus;
  mode: "initial" | "settings";
  isSaving: boolean;
  errorMessage: string | null;
  onBootstrapSubmit: (payload: BootstrapInitializePayload) => Promise<boolean>;
  onQWeatherSubmit?: (payload: QWeatherApiKeyPayload) => Promise<boolean>;
};

/**
 * 渲染统一服务密钥管理面板。
 *
 * Args:
 *   title: 面板标题。
 *   description: 当前页面语境下的说明文案。
 *   status: 后端返回的服务密钥布尔状态。
 *   mode: initial 表示首次初始化，settings 表示设置页更新。
 *   isSaving: 当前是否有密钥保存请求进行中。
 *   errorMessage: 父组件传入的保存错误。
 *   onBootstrapSubmit: 提交必需服务密钥的回调，返回是否保存成功。
 *   onQWeatherSubmit: 单独验证并保存和风天气服务配置的回调，返回是否保存成功。
 *
 * Returns:
 *   可填写和风天气 API Host/API Key 的表单卡片。
 */
export function ServiceKeysPanel({
  title,
  description,
  status,
  mode,
  isSaving,
  errorMessage,
  onBootstrapSubmit,
  onQWeatherSubmit
}: ServiceKeysPanelProps): JSX.Element {
  const [qweatherApiKey, setQweatherApiKey] = useState("");
  const [qweatherApiHost, setQweatherApiHost] = useState("");

  const trimmedQweatherApiKey = qweatherApiKey.trim();
  const trimmedQweatherApiHost = qweatherApiHost.trim();
  const hasPartialQweatherConfig =
    (trimmedQweatherApiKey.length > 0 && trimmedQweatherApiHost.length === 0) ||
    (trimmedQweatherApiHost.length > 0 && trimmedQweatherApiKey.length === 0);
  const isBootstrapSubmitDisabled =
    isSaving || hasPartialQweatherConfig || !status.llm.is_ready || !status.voice.is_ready;
  const isQWeatherSubmitDisabled =
    isSaving || trimmedQweatherApiKey.length === 0 || trimmedQweatherApiHost.length === 0 || onQWeatherSubmit === undefined;
  const bootstrapSubmitLabel = "完成初始化";

  /**
   * 提交必需服务密钥。
   *
   * Args:
   *   event: 浏览器表单提交事件。
   *
   * Returns:
   *   无返回值；保存成功后清空和风天气输入框，失败时保留用户已输入内容。
   */
  async function handleBootstrapSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (isBootstrapSubmitDisabled) {
      return;
    }

    const succeeded = await onBootstrapSubmit({
      qweather_api_key: trimmedQweatherApiKey || null,
      qweather_api_host: trimmedQweatherApiHost || null
    });
    if (succeeded && trimmedQweatherApiKey) {
      setQweatherApiKey("");
      setQweatherApiHost("");
    }
  }

  /**
   * 验证并保存可选和风天气密钥。
   *
   * Returns:
   *   无返回值；保存成功后清空输入框，失败时保留用户已输入内容。
   */
  async function handleQWeatherSubmit(): Promise<void> {
    if (isQWeatherSubmitDisabled || onQWeatherSubmit === undefined) {
      return;
    }

    const succeeded = await onQWeatherSubmit({
      qweather_api_key: trimmedQweatherApiKey,
      qweather_api_host: trimmedQweatherApiHost
    });
    if (succeeded) {
      setQweatherApiKey("");
      setQweatherApiHost("");
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {errorMessage !== null ? (
          <p className="text-sm font-medium text-destructive">密钥保存失败：{errorMessage}</p>
        ) : null}

        <form className="space-y-2" onSubmit={(event) => {
          if (mode === "initial") { void handleBootstrapSubmit(event); return; }
          event.preventDefault();
          void handleQWeatherSubmit();
        }}>
          {mode === "initial" ? <>
          <SettingRow
            description="模型配置由上方 LLM API 配置模块负责；这里仅显示是否已满足初始化要求。"
            title="LLM 配置门禁"
          >
            <Badge variant={status.llm.is_ready ? "success" : "secondary"}>
              {status.llm.is_ready ? "已完成" : "未完成"}
            </Badge>
          </SettingRow>
          <p className="text-sm text-muted-foreground">内置语音随 NearMate 授权提供，无需另购 SDK Key。</p>
          </> : null}
          <SettingRow
            description="可选；需要填写和风控制台分配的 API Host 与 API Key，配置后天气工具才能使用实时数据。"
            layout="field"
            title="和风天气服务配置"
          >
            <div className="space-y-2">
              <div className="flex justify-end">
                <Badge variant={status.has_qweather_api_key ? "success" : "secondary"}>
                  {status.has_qweather_api_key ? "已配置" : "未配置"}
                </Badge>
              </div>
              <label className="block text-xs font-medium">API Host
              <Input
                aria-label="和风天气 API Host"
                disabled={isSaving}
                onChange={(event) => setQweatherApiHost(event.target.value)}
                placeholder="abc1234xyz.def.qweatherapi.com"
                value={qweatherApiHost}
              />
              </label>
              <label className="block text-xs font-medium">API Key
              <Input
                aria-label="和风天气 API Key"
                disabled={isSaving}
                onChange={(event) => setQweatherApiKey(event.target.value)}
                type="password"
                value={qweatherApiKey}
              />
              </label>
              {mode === "settings" ? (
                <div className="flex justify-end">
                  <Button
                    disabled={isQWeatherSubmitDisabled}
                    type="submit"
                  >
                    {isSaving ? "验证中" : "验证并保存"}
                  </Button>
                </div>
              ) : null}
            </div>
          </SettingRow>

          {mode === "initial" ? <div className="flex justify-end pt-2">
            <Button disabled={isBootstrapSubmitDisabled} type="submit">
              {isSaving ? "保存中" : bootstrapSubmitLabel}
            </Button>
          </div> : null}
        </form>
      </CardContent>
    </Card>
  );
}
