import { type FormEvent, useEffect, useState } from "react";

import { EmptyState } from "@/components/layout/EmptyState";
import { SettingRow } from "@/components/layout/SettingRow";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type {
  StreamVoxConfigPayload,
  StreamVoxCreateRolePayload,
  StreamVoxPreviewRolePayload,
  StreamVoxRuntimeState
} from "../../api/contracts/streamvox";

export type StreamVoxRolePanelProps = {
  runtime: StreamVoxRuntimeState | null;
  isSaving: boolean;
  onSaveConfig: (payload: StreamVoxConfigPayload) => Promise<void>;
  onCreateRole: (payload: StreamVoxCreateRolePayload) => Promise<void | boolean>;
  onPreviewRole: (payload: StreamVoxPreviewRolePayload) => Promise<Blob | null>;
  onDeleteRole: (roleName: string) => Promise<void>;
  onCaptureCurrentVoice: () => Promise<void>;
};

/**
 * 渲染 StreamVox 音色管理面板。
 *
 * Args:
 *   runtime: 当前 StreamVox 运行状态。
 *   isSaving: 是否正在保存配置。
 *   onSaveConfig: 保存默认音色。
 *   onCreateRole: 创建新音色。
 *   onPreviewRole: 试听已有音色。
 *   onDeleteRole: 删除已有音色。
 *   onCaptureCurrentVoice: 捕获当前运行音色回角色卡。
 *
 * Returns:
 *   音色创建、试听和角色管理界面。
 */
export function StreamVoxRolePanel({
  runtime,
  isSaving,
  onSaveConfig,
  onCreateRole,
  onPreviewRole,
  onDeleteRole,
  onCaptureCurrentVoice
}: StreamVoxRolePanelProps): JSX.Element {
  const [newRoleName, setNewRoleName] = useState("");
  const [newRolePromptText, setNewRolePromptText] = useState("");
  const [newRoleAudioFile, setNewRoleAudioFile] = useState<File | null>(null);
  const [uploadedAudioUrl, setUploadedAudioUrl] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState("你好，这是 StreamVox 音色试听。");
  const [previewAudioUrl, setPreviewAudioUrl] = useState<string | null>(null);

  useEffect(() => {
    if (newRoleAudioFile === null) {
      setUploadedAudioUrl(null);
      return;
    }

    const objectUrl = URL.createObjectURL(newRoleAudioFile);
    setUploadedAudioUrl(objectUrl);
    return () => {
      revokeObjectUrlIfSupported(objectUrl);
    };
  }, [newRoleAudioFile]);

  useEffect(() => {
    return () => {
      if (previewAudioUrl !== null) {
        revokeObjectUrlIfSupported(previewAudioUrl);
      }
    };
  }, [previewAudioUrl]);

  async function handleCreateRole(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (isSaving || newRoleAudioFile === null || !newRoleName.trim() || !newRolePromptText.trim()) {
      return;
    }

    const succeeded = await onCreateRole({
      role_name: newRoleName.trim(),
      prompt_text: newRolePromptText.trim(),
      audio_file: newRoleAudioFile
    });
    // 失败时保留名称、文本和文件，用户可以直接修正后重试。
    if (succeeded === false) return;
    setNewRoleName("");
    setNewRolePromptText("");
    setNewRoleAudioFile(null);
  }

  async function handlePreviewRole(role: string): Promise<void> {
    if (!previewText.trim()) {
      return;
    }

    const previewBlob = await onPreviewRole({
      role_name: role,
      text: previewText.trim()
    });
    if (previewBlob === null) {
      return;
    }

    setPreviewAudioUrl((currentUrl) => {
      if (currentUrl !== null) {
        revokeObjectUrlIfSupported(currentUrl);
      }
      return URL.createObjectURL(previewBlob);
    });
  }

  async function handleUseRoleAsDefault(role: string): Promise<void> {
    if (isSaving || runtime === null || role === runtime.role_name) {
      return;
    }

    await onSaveConfig({
      model: runtime.model,
      device: runtime.device,
      llama_backend: runtime.llama_backend,
      role_name: role,
      streamvox_seed: runtime.streamvox_seed,
      inference_options: runtime.inference_options
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>音色管理</CardTitle>
        <CardDescription>上传参考音频创建音色，并管理当前模型下的默认音色。</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <details className="rounded-lg border px-4 py-3">
          <summary className="cursor-pointer text-sm font-medium">新建音色</summary>
        <form className="space-y-2" onSubmit={(event) => void handleCreateRole(event)}>
          <SettingRow description="在当前模型下创建一个新音色，不会自动切换当前使用音色。" layout="field" title="新音色名称">
            <Input aria-label="新音色名称" disabled={isSaving} onChange={(event) => setNewRoleName(event.target.value)} value={newRoleName} />
          </SettingRow>
          <SettingRow description="填写参考音频中说出的原文，帮助模型准确还原音色。" layout="field" title="参考音频文本">
            <Textarea aria-label="参考音频文本" disabled={isSaving} onChange={(event) => setNewRolePromptText(event.target.value)} rows={3} value={newRolePromptText} />
          </SettingRow>
          <SettingRow description="本轮支持单参考音频；创建成功后音色缓存由当前模型独立维护。" layout="field" title="参考音频文件">
            <input
              accept="audio/*"
              aria-label="参考音频文件"
              className="w-full text-sm text-muted-foreground file:mr-3 file:rounded-md file:border file:border-input file:bg-card file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-foreground hover:file:bg-accent"
              disabled={isSaving}
              onChange={(event) => setNewRoleAudioFile(event.target.files?.[0] ?? null)}
              type="file"
            />
          </SettingRow>
          {uploadedAudioUrl !== null ? (
            <audio aria-label="上传原音频试听" className="w-full" controls src={uploadedAudioUrl} />
          ) : null}
          <div className="flex flex-wrap gap-2 pt-2">
            <Button disabled={isSaving || newRoleAudioFile === null || !newRoleName.trim() || !newRolePromptText.trim()} type="submit">
              创建音色
            </Button>
          </div>
        </form>
        </details>

        <div className="space-y-4 border-t pt-6">
          {runtime === null ? (
            <EmptyState>正在读取 StreamVox 运行状态。</EmptyState>
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1 rounded-lg border bg-muted/30 p-4">
                  <span className="text-xs text-muted-foreground">当前模型</span>
                  <strong className="block text-sm font-semibold">{runtime.model}</strong>
                  <small className="text-xs text-muted-foreground">
                    {runtime.device} · {runtime.llama_backend ?? "默认"}
                  </small>
                </div>
                <div className="space-y-1 rounded-lg border bg-muted/30 p-4">
                  <span className="text-xs text-muted-foreground">采样率</span>
                  <strong className="block text-sm font-semibold">{runtime.sample_rate} Hz</strong>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  aria-label="更新到当前角色卡"
                  disabled={isSaving}
                  onClick={() => void onCaptureCurrentVoice()}
                  type="button"
                >
                  更新到当前角色卡
                </Button>
              </div>
              <SettingRow description="点击任一角色试听时，会用这里的文本生成一段 WAV 音频。" layout="field" title="推理试听文本">
                <Textarea aria-label="推理试听文本" disabled={isSaving} onChange={(event) => setPreviewText(event.target.value)} rows={2} value={previewText} />
              </SettingRow>
              {previewAudioUrl !== null ? (
                <audio aria-label="推理试听音频" className="w-full" controls src={previewAudioUrl} />
              ) : null}
              <div aria-label="StreamVox 当前模型音色列表" className="space-y-3">
                {runtime.roles.map((role) => {
                  const isCurrentRole = role === runtime.role_name;

                  return (
                    <article className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4" key={role}>
                      <div className="space-y-1">
                        <h3 className="text-sm font-medium">{role}</h3>
                        <p className="text-sm text-muted-foreground">
                          {isCurrentRole ? "当前使用" : "可删除"}
                          {isCurrentRole ? " · 默认音色" : ""}
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button aria-label={`试听 ${role}`} disabled={isSaving || !previewText.trim()} onClick={() => void handlePreviewRole(role)} type="button" variant="outline">
                          试听
                        </Button>
                        <Button aria-label={`设为默认音色 ${role}`} disabled={isSaving || isCurrentRole} onClick={() => void handleUseRoleAsDefault(role)} type="button" variant="outline">
                          设为默认音色
                        </Button>
                        <Button aria-label={`删除 ${role}`} disabled={isSaving || isCurrentRole} onClick={() => void onDeleteRole(role)} type="button" variant="outline">
                          删除
                        </Button>
                      </div>
                    </article>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function revokeObjectUrlIfSupported(objectUrl: string): void {
  if (typeof URL.revokeObjectURL === "function") {
    URL.revokeObjectURL(objectUrl);
  }
}
