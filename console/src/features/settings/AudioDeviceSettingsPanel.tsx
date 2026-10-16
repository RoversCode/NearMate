import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  type AudioDevice,
  type AudioDeviceApplyResult,
  type AudioDeviceSelectionPayload,
  type AudioDevicesPayload,
  type AudioDeviceTestResult
} from "../../api/contracts/audioDevices";
import { type ConsoleSettings } from "../../api/contracts/settings";
import {
  fetchAudioDevices,
  testAudioDevice,
  updateAudioDeviceSettings
} from "../../api/services/audioDevices";
import { fetchSettings } from "../../api/services/settings";
import { formatSavedRefreshWarning } from "../../lib/mutationFeedback";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { SettingRow } from "@/components/layout/SettingRow";

type AudioDeviceSettingsPanelProps = {
  applyResult: AudioDeviceApplyResult | null;
  baseUrl: string;
  settings: ConsoleSettings;
  onSaved: (settings: ConsoleSettings) => void;
};

/**
 * 渲染音频设备设置面板。
 *
 * Args:
 *   applyResult: desktop 回传的最近一次音频设备应用结果。
 *   baseUrl: 控制面 HTTP 根地址。
 *   settings: 后端当前设置快照。
 *   onSaved: 保存成功后同步父级设置快照的回调。
 *
 * Returns:
 *   可刷新设备清单的选择、测试和保存界面；刷新保留尚未保存的选择。
 */
export function AudioDeviceSettingsPanel({ applyResult, baseUrl, settings, onSaved }: AudioDeviceSettingsPanelProps): JSX.Element {
  const [devices, setDevices] = useState<AudioDevicesPayload | null>(null);
  const [mode, setMode] = useState<"system_default" | "manual">(settings.audio_device_mode);
  const [microphoneId, setMicrophoneId] = useState(settings.audio_input_device_id ?? "");
  const [speakerLoopbackId, setSpeakerLoopbackId] = useState(settings.audio_output_reference_device_id ?? "");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [pendingApplyRequestId, setPendingApplyRequestId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<AudioDeviceTestResult | null>(null);
  const [testCountdown, setTestCountdown] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [deviceLoadError, setDeviceLoadError] = useState<string | null>(null);
  const [refreshSequence, setRefreshSequence] = useState(0);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);

  useEffect(() => {
    setMode(settings.audio_device_mode);
    setMicrophoneId(settings.audio_input_device_id ?? "");
    setSpeakerLoopbackId(settings.audio_output_reference_device_id ?? "");
  }, [settings.audio_device_mode, settings.audio_input_device_id, settings.audio_output_reference_device_id]);

  useEffect(() => {
    let isActive = true;
    setIsLoading(true);
    setDeviceLoadError(null);

    void fetchAudioDevices(baseUrl)
      .then((payload) => {
        if (!isActive) {
          return;
        }
        setDevices(payload);
        // 手动刷新只更新设备清单，不能把用户尚未保存的选择替换成服务端旧值。
        if (refreshSequence === 0) {
          setMode(payload.selected.mode);
          setMicrophoneId(payload.selected.microphone_id ?? "");
          setSpeakerLoopbackId(payload.selected.speaker_loopback_id ?? "");
        }
      })
      .catch((error: unknown) => {
        if (isActive) {
          setDeviceLoadError(error instanceof Error ? error.message : "读取音频设备失败");
        }
      })
      .finally(() => {
        if (isActive) {
          setIsLoading(false);
        }
      });

    return () => {
      isActive = false;
    };
  }, [baseUrl, refreshSequence]);

  useEffect(() => {
    if (!isTesting) {
      return undefined;
    }

    setTestCountdown(3);
    const timerId = window.setInterval(() => {
      setTestCountdown((currentValue) => {
        if (currentValue === null || currentValue <= 1) {
          return 1;
        }
        return currentValue - 1;
      });
    }, 1000);

    return () => {
      window.clearInterval(timerId);
    };
  }, [isTesting]);

  useEffect(() => {
    if (pendingApplyRequestId === null) {
      return undefined;
    }

    const timeoutId = window.setTimeout(() => {
      setPendingApplyRequestId(null);
      setStatusMessage("已保存，但未收到桌宠应用结果；请确认 desktop 正在运行");
    }, 8000);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [pendingApplyRequestId]);

  useEffect(() => {
    if (pendingApplyRequestId === null || applyResult?.request_id !== pendingApplyRequestId) {
      return;
    }

    setPendingApplyRequestId(null);
    if (applyResult.status === "applied") {
      setStatusMessage("音频设备已应用到桌宠语音会话");
      setErrorMessage(null);
      return;
    }

    setStatusMessage(null);
    setErrorMessage(`已保存，但应用失败：${applyResult.reason ?? "桌宠语音会话重建失败"}`);
  }, [applyResult, pendingApplyRequestId]);

  const selectedMicrophone = useMemo(
    () => devices?.microphones.find((device) => (device.id ?? device.name) === microphoneId) ?? null,
    [devices, microphoneId]
  );
  const selectedLoopback = useMemo(
    () => devices?.speaker_loopbacks.find((device) => (device.id ?? device.name) === speakerLoopbackId) ?? null,
    [devices, speakerLoopbackId]
  );
  const defaultMicrophone = useMemo(
    () => devices?.microphones.find((device) => device.is_default) ?? null,
    [devices]
  );
  const defaultLoopback = useMemo(
    () => devices?.speaker_loopbacks.find((device) => device.is_default) ?? null,
    [devices]
  );
  const displayedMicrophoneId = mode === "system_default" ? defaultMicrophone?.id ?? defaultMicrophone?.name ?? "" : microphoneId;
  const displayedSpeakerLoopbackId =
    mode === "system_default" ? defaultLoopback?.id ?? defaultLoopback?.name ?? "" : speakerLoopbackId;
  const pendingSelection = useMemo<AudioDeviceSelectionPayload>(
    () => ({
      mode,
      microphone_id: mode === "manual" ? selectedMicrophone?.id ?? null : null,
      microphone_name: mode === "manual" ? selectedMicrophone?.name ?? null : null,
      speaker_loopback_id: mode === "manual" ? selectedLoopback?.id ?? null : null,
      speaker_loopback_name: mode === "manual" ? selectedLoopback?.name ?? null : null
    }),
    [mode, selectedLoopback, selectedMicrophone]
  );
  const isSelectionUnchanged =
    pendingSelection.mode === settings.audio_device_mode &&
    pendingSelection.microphone_id === settings.audio_input_device_id &&
    pendingSelection.microphone_name === settings.audio_input_device_name &&
    pendingSelection.speaker_loopback_id === settings.audio_output_reference_device_id &&
    pendingSelection.speaker_loopback_name === settings.audio_output_reference_device_name;
  const isManualMicrophoneMissing = mode === "manual" && selectedMicrophone === null;
  const isApplying = pendingApplyRequestId !== null;
  const areDevicesBusy = isLoading || isSaving || isApplying || isTesting;
  const testButtonDisabled = areDevicesBusy || devices === null || isManualMicrophoneMissing;
  const submitButtonDisabled = areDevicesBusy || devices === null || isManualMicrophoneMissing || isSelectionUnchanged;

  async function handleTestMicrophone(): Promise<void> {
    if (testButtonDisabled) {
      return;
    }

    setIsTesting(true);
    setTestResult(null);
    setStatusMessage(null);
    setErrorMessage(null);
    try {
      const result = await testAudioDevice(baseUrl, {
        microphone_id: mode === "manual" ? selectedMicrophone?.id ?? null : null,
        microphone_name: mode === "manual" ? selectedMicrophone?.name ?? null : null
      });
      setTestResult(result);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "测试麦克风失败");
    } finally {
      setIsTesting(false);
      setTestCountdown(null);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (areDevicesBusy || devices === null) return;
    if (isManualMicrophoneMissing) {
      setErrorMessage("请选择麦克风后再更改音频设备");
      return;
    }
    if (isSelectionUnchanged) {
      return;
    }

    setIsSaving(true);
    setErrorMessage(null);
    setStatusMessage(null);
    setTestResult(null);

    try {
      const result = await updateAudioDeviceSettings(baseUrl, pendingSelection);
      // 音频接口也会推进全局 settings_revision；必须采用提交后快照，不能用旧快照
      // 拼出一个“看起来更新了音频、实际版本没变”的对象，否则下一次快捷键保存会 409。
      let committedSettings = result.settings;
      let refreshWarning: string | null = null;
      if (committedSettings === undefined) {
        try {
          committedSettings = await fetchSettings(baseUrl);
        } catch (refreshError) {
          // 旧 Backend 没有在 POST 响应里附带 revision；回读失败时仍然保留
          // 已经成功的设备切换，但不伪造一个带旧 revision 的本地快照。
          refreshWarning = formatSavedRefreshWarning(refreshError, "设置快照暂时不可用");
        }
      }
      if (committedSettings !== undefined) {
        onSaved(committedSettings);
      }
      setPendingApplyRequestId(result.request_id);
      setStatusMessage(refreshWarning === null ? "已保存，正在应用到桌宠语音会话" : `${refreshWarning}；正在应用到桌宠语音会话`);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "更改音频设备失败");
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1.5">
          <CardTitle>音频设备</CardTitle>
          <CardDescription>选择语音输入设备，连接新设备后可刷新清单。</CardDescription>
        </div>
        <Button
          disabled={areDevicesBusy}
          onClick={() => setRefreshSequence((current) => current + 1)}
          size="sm"
          type="button"
          variant="outline"
        >
          {isLoading ? "读取中…" : deviceLoadError !== null ? "重新读取设备" : "刷新设备"}
        </Button>
      </CardHeader>
      <CardContent className="space-y-4">
        {deviceLoadError ? <p className="text-sm font-medium text-destructive" role="alert">{deviceLoadError}</p> : null}
        {errorMessage ? <p className="text-sm font-medium text-destructive" role="alert">{errorMessage}</p> : null}
        {statusMessage ? (
          <p className="rounded-lg border bg-muted/50 px-4 py-3 text-sm text-muted-foreground" role="status">{statusMessage}</p>
        ) : null}
        <form className="space-y-2" onSubmit={(event) => void handleSubmit(event)}>
          <SettingRow
            description="系统默认会自动尝试备用麦克风；手动选择会固定使用你保存的设备。"
            layout="field"
            title="选择模式"
          >
            <Select
              aria-label="音频设备选择模式"
              disabled={areDevicesBusy || devices === null}
              onChange={(event) => setMode(event.target.value as "system_default" | "manual")}
              value={mode}
            >
              <option value="system_default">使用系统默认</option>
              <option value="manual">手动选择设备</option>
            </Select>
          </SettingRow>
          <DeviceSelectRow
            devices={devices?.microphones ?? []}
            disabled={areDevicesBusy || mode !== "manual"}
            label="麦克风"
            onChange={setMicrophoneId}
            value={displayedMicrophoneId}
          />
          <DeviceSelectRow
            devices={devices?.speaker_loopbacks ?? []}
            disabled={areDevicesBusy || mode !== "manual"}
            label="扬声器参考"
            onChange={setSpeakerLoopbackId}
            value={displayedSpeakerLoopbackId}
          />
          <SettingRow
            description={mode === "manual" ? "录制当前选择的麦克风三秒，并返回音量结果和回放。" : "录制系统默认麦克风三秒，并返回音量结果和回放。"}
            title="设备测试"
          >
            <div className="flex flex-wrap gap-2">
              <Button disabled={testButtonDisabled} onClick={() => void handleTestMicrophone()} type="button" variant="outline">
                {isTesting ? "录音中" : "开始 3 秒测试"}
              </Button>
              <Button disabled={submitButtonDisabled} type="submit">
                {isSaving ? "更改中" : isApplying ? "应用中" : "更改音频设备"}
              </Button>
            </div>
          </SettingRow>
          {isManualMicrophoneMissing ? (
            <p className="text-sm text-warning">请选择麦克风后再开始测试或更改音频设备。</p>
          ) : null}
          {isTesting ? (
            <section aria-live="polite" className="flex items-center gap-3 rounded-lg border border-primary/30 bg-accent px-4 py-3">
              <strong className="text-sm font-semibold text-accent-foreground">请现在说话</strong>
              <span className="text-sm text-muted-foreground">正在录音 {testCountdown ?? 3} 秒</span>
            </section>
          ) : null}
          {testResult !== null ? <AudioTestResultPanel result={testResult} /> : null}
        </form>
      </CardContent>
    </Card>
  );
}

/**
 * 渲染单个设备下拉选择行。
 *
 * Args:
 *   devices: 可选设备列表。
 *   disabled: 是否禁用选择。
 *   label: 控件的可访问名称。
 *   onChange: 选择变化回调。
 *   value: 当前选中的设备标识。
 *
 * Returns:
 *   带说明文字的设备选择设置行。
 */
function DeviceSelectRow({
  devices,
  disabled,
  label,
  onChange,
  value
}: {
  devices: AudioDevice[];
  disabled: boolean;
  label: string;
  onChange: (value: string) => void;
  value: string;
}): JSX.Element {
  return (
    <SettingRow
      description={devices.length > 0 ? "选择要用于实时语音链路的设备。" : "暂未发现可选择设备。"}
      layout="field"
      title={label}
    >
      <Select aria-label={label} disabled={disabled || devices.length === 0} onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">未选择</option>
        {devices.map((device) => (
          <option key={device.id ?? device.name} value={device.id ?? device.name}>
            {device.name} · {device.status}
          </option>
        ))}
      </Select>
    </SettingRow>
  );
}

/**
 * 渲染麦克风测试的结果面板。
 *
 * Args:
 *   result: 后端返回的测试结论、音量指标和回放音频。
 *
 * Returns:
 *   包含结论摘要、电平表和回放器的面板。
 */
function AudioTestResultPanel({ result }: { result: AudioDeviceTestResult }): JSX.Element {
  const isAvailable = result.status === "available";
  const audioSource = isAvailable && result.audio_wav_b64 ? `data:audio/wav;base64,${result.audio_wav_b64}` : null;
  const peakPercent = Math.round(result.peak_level * 100);
  const rmsPercent = Math.round(result.rms_level * 100);

  return (
    <section className="space-y-4 rounded-lg border bg-muted/40 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <strong className="text-sm font-semibold">{isAvailable ? levelStatusLabel(result.level_status, audioSource !== null) : `麦克风不可用：${result.error_message || result.status}`}</strong>
        <span className="text-xs text-muted-foreground">{isAvailable ? `${result.duration_ms} ms · ${result.sample_rate} Hz` : result.error_type ?? "device_error"}</span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <AudioLevelMeter label="峰值" value={peakPercent} />
        <AudioLevelMeter label="平均" value={rmsPercent} />
      </div>
      {audioSource !== null ? (
        <audio aria-label="麦克风测试录音" className="w-full" controls src={audioSource} />
      ) : isAvailable ? (
        <p className="text-sm text-muted-foreground">麦克风可用，但没有收到可回放录音。</p>
      ) : null}
    </section>
  );
}

/**
 * 渲染单条音量电平表。
 *
 * Args:
 *   label: 指标名称（峰值/平均）。
 *   value: 0-100 的百分比数值，越界会被收敛。
 *
 * Returns:
 *   标签、进度轨道和百分比读数组成的电平行。
 */
function AudioLevelMeter({ label, value }: { label: string; value: number }): JSX.Element {
  const normalizedValue = Math.max(0, Math.min(100, value));

  return (
    <div className="flex items-center gap-3">
      <span className="w-8 shrink-0 text-xs text-muted-foreground">{label}</span>
      <div aria-hidden="true" className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${normalizedValue}%` }} />
      </div>
      <strong className="w-10 shrink-0 text-right text-xs font-semibold tabular-nums">{normalizedValue}%</strong>
    </div>
  );
}

function levelStatusLabel(status: AudioDeviceTestResult["level_status"], hasAudio: boolean): string {
  if (!hasAudio) {
    return "麦克风可用，但回放音频为空";
  }
  if (status === "silent") {
    return "未检测到明显输入";
  }
  if (status === "low") {
    return "麦克风音量偏低";
  }
  if (status === "clipping") {
    return "麦克风音量过高";
  }
  return "麦克风音量正常";
}
