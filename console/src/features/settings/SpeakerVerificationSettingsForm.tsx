import { useEffect, useRef, useState } from "react";

import {
  type SpeakerRecordingProgress,
  type SpeakerVerificationProfile
} from "../../api/contracts/speakerVerification";
import type { ConsoleSettings } from "../../api/contracts/settings";
import {
  createSpeakerVerificationSample,
  deleteSpeakerVerificationSample,
  fetchSpeakerVerificationProfile,
  updateSpeakerVerificationProfile
} from "../../api/services/speakerVerification";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { EmptyState } from "@/components/layout/EmptyState";
import { formatSavedRefreshWarning } from "../../lib/mutationFeedback";
import { float32ToPcm16 } from "./speakerRecordingAudio";

type SpeakerVerificationSettingsFormProps = {
  baseUrl: string;
  isSavingSettings: boolean;
  /** 说话人配置成功后刷新共享的普通设置快照。 */
  onSettingsSnapshotRefresh?: (candidateSettings?: ConsoleSettings) => Promise<unknown>;
};

type RecordingState = {
  status: "idle" | "recording" | "saving";
  replacingSampleId: string | null;
  speechSeconds: number;
  errorMessage: string | null;
};

const initialRecordingState: RecordingState = {
  status: "idle",
  replacingSampleId: null,
  speechSeconds: 0,
  errorMessage: null
};

/**
 * 渲染说话人确认白名单设置卡片。
 *
 * Args:
 *   baseUrl: 控制面 HTTP 根地址。
 *   isSavingSettings: 父级普通设置是否正在保存。
 *
 * Returns:
 *   说话人确认开关、可取消的录音、试听、替换和删除控件；写入时阻止重复操作。
 */
export function SpeakerVerificationSettingsForm({
  baseUrl,
  isSavingSettings,
  onSettingsSnapshotRefresh
}: SpeakerVerificationSettingsFormProps): JSX.Element {
  const [profile, setProfile] = useState<SpeakerVerificationProfile | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [recordingState, setRecordingState] = useState<RecordingState>(initialRecordingState);
  const [isSampleListExpanded, setIsSampleListExpanded] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [pendingWrite, setPendingWrite] = useState<string | null>(null);
  const writeInProgressRef = useRef(false);
  const recordingGenerationRef = useRef(0);
  const recordingInProgressRef = useRef(false);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const websocketRef = useRef<WebSocket | null>(null);
  const replacingSampleIdRef = useRef<string | null>(null);

  useEffect(() => {
    let isActive = true;
    setErrorMessage(null);
    setStatusMessage(null);

    void fetchSpeakerVerificationProfile(baseUrl)
      .then((nextProfile) => {
        if (isActive) {
          setProfile(nextProfile);
        }
      })
      .catch((error) => {
        if (isActive) {
          setErrorMessage(error instanceof Error ? error.message : "未知说话人确认状态读取错误");
        }
      });

    return () => {
      isActive = false;
      stopRecordingResources();
    };
  }, [baseUrl, loadAttempt]);

  const canRecord = profile !== null && profile.samples.length < profile.max_sample_count;
  const isBusy = pendingWrite !== null || recordingState.status !== "idle" || isSavingSettings;
  const canToggle = profile !== null && (profile.enabled || profile.can_enable) && !isBusy;

  /**
   * 切换说话人确认开关。
   *
   * Args:
   *   enabled: 目标开关状态。
   *
   * Returns:
   *   无；保存成功后刷新 profile。
   */
  async function handleToggle(enabled: boolean): Promise<void> {
    if (!canToggle || profile === null || writeInProgressRef.current) {
      return;
    }

    writeInProgressRef.current = true;
    setPendingWrite("toggle");
    try {
      setErrorMessage(null);
      setStatusMessage(null);
      const nextProfile = await updateSpeakerVerificationProfile(baseUrl, enabled);
      setProfile(nextProfile);
      const refreshWarning = await refreshSettingsSnapshotAfterMutation(nextProfile.settings);
      if (refreshWarning !== null) {
        setStatusMessage(refreshWarning);
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知说话人确认保存错误");
    } finally {
      writeInProgressRef.current = false;
      setPendingWrite(null);
    }
  }

  /**
   * 删除指定样本，并在服务端返回前锁住其他写入。
   *
   * Args:
   *   sampleId: 要删除的录音编号。
   *
   * Returns:
   *   删除后显示服务端返回的最新样本列表。
   */
  async function handleDelete(sampleId: string): Promise<void> {
    if (isBusy || writeInProgressRef.current) return;
    writeInProgressRef.current = true;
    setPendingWrite(sampleId);
    try {
      setErrorMessage(null);
      setStatusMessage(null);
      const nextProfile = await deleteSpeakerVerificationSample(baseUrl, sampleId);
      setProfile(nextProfile);
      const refreshWarning = await refreshSettingsSnapshotAfterMutation(nextProfile.settings);
      if (refreshWarning !== null) {
        setStatusMessage(refreshWarning);
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "未知录音删除错误");
    } finally {
      writeInProgressRef.current = false;
      setPendingWrite(null);
    }
  }

  /**
   * 请求麦克风并开始录音；取消后到达的许可和旧连接回调都会被忽略。
   *
   * Args:
   *   replacingSampleId: 要替换的已有录音编号；为空时创建新样本。
   *
   * Returns:
   *   有效人声达到要求后自动保存，用户也可提前取消。
   */
  async function handleStartRecording(replacingSampleId: string | null = null): Promise<void> {
    if (profile === null || isBusy || writeInProgressRef.current || recordingInProgressRef.current) {
      return;
    }
    if (replacingSampleId === null && !canRecord) {
      return;
    }

    const recordingGeneration = ++recordingGenerationRef.current;
    recordingInProgressRef.current = true;
    try {
      setErrorMessage(null);
      setStatusMessage(null);
      setRecordingState({
        status: "recording",
        replacingSampleId,
        speechSeconds: 0,
        errorMessage: null
      });
      replacingSampleIdRef.current = replacingSampleId;
      const mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      // 浏览器许可弹窗无法主动关闭；取消后用户才授权时，立即释放迟到的麦克风流。
      if (recordingGeneration !== recordingGenerationRef.current) {
        mediaStream.getTracks().forEach((track) => track.stop());
        return;
      }
      mediaStreamRef.current = mediaStream;
      const audioContext = new AudioContext();
      audioContextRef.current = audioContext;
      sourceRef.current = audioContext.createMediaStreamSource(mediaStream);
      processorRef.current = audioContext.createScriptProcessor(4096, 1, 1);
      const websocket = new WebSocket(buildSpeakerRecordingWebSocketUrl(baseUrl));
      websocket.binaryType = "arraybuffer";
      websocketRef.current = websocket;

      websocket.onmessage = (event) => {
        if (recordingGeneration !== recordingGenerationRef.current) return;
        try {
          void handleRecordingProgress(JSON.parse(event.data) as SpeakerRecordingProgress);
        } catch {
          stopRecordingResources();
          setRecordingState({ ...initialRecordingState, errorMessage: "录音服务返回了无法读取的进度，请重新录制。" });
        }
      };
      websocket.onerror = () => {
        if (recordingGeneration !== recordingGenerationRef.current) return;
        setRecordingState((currentState) => ({
          ...currentState,
          status: "idle",
          errorMessage: "录音校验连接异常"
        }));
        stopRecordingResources();
      };
      websocket.onclose = () => {
        if (recordingGeneration !== recordingGenerationRef.current) return;
        stopRecordingResources();
        setRecordingState({ ...initialRecordingState, errorMessage: "录音连接已断开，请重新录制。" });
      };
      websocket.onopen = () => {
        if (recordingGeneration !== recordingGenerationRef.current) return;
        const processor = processorRef.current;
        const source = sourceRef.current;
        if (processor === null || source === null) {
          return;
        }

        processor.onaudioprocess = (event) => {
          const pcm16 = float32ToPcm16(event.inputBuffer.getChannelData(0), audioContext.sampleRate);
          if (websocket.readyState === WebSocket.OPEN) {
            websocket.send(pcm16);
          }
        };
        source.connect(processor);
        processor.connect(audioContext.destination);
      };
    } catch (error) {
      if (recordingGeneration !== recordingGenerationRef.current) return;
      stopRecordingResources();
      setRecordingState({
        ...initialRecordingState,
        errorMessage: error instanceof Error ? error.message : "未知录音启动错误"
      });
    }
  }

  async function handleRecordingProgress(progress: SpeakerRecordingProgress): Promise<void> {
    if (progress.type === "error" || progress.type === "rejected") {
      stopRecordingResources();
      setRecordingState({
        ...initialRecordingState,
        errorMessage: progress.message ?? "录音未通过后端校验"
      });
      return;
    }

    const nextSpeechSeconds = progress.speech_seconds ?? 0;
    setRecordingState((currentState) => ({
      ...currentState,
      speechSeconds: nextSpeechSeconds
    }));

    if (progress.type !== "completed") {
      return;
    }

    const replacingSampleId = replacingSampleIdRef.current;
    const recordingId = progress.recording_id;
    stopRecordingResources();
    if (recordingId === undefined || recordingId.length === 0) {
      setRecordingState({
        ...initialRecordingState,
        errorMessage: "后端未返回录音会话编号"
      });
      return;
    }
    setRecordingState((currentState) => ({
      ...currentState,
      status: "saving",
      speechSeconds: nextSpeechSeconds
    }));
    try {
      const nextProfile = await createSpeakerVerificationSample(baseUrl, {
        recording_id: recordingId,
        replace_sample_id: replacingSampleId
      });
      setProfile(nextProfile);
      const refreshWarning = await refreshSettingsSnapshotAfterMutation(nextProfile.settings);
      if (refreshWarning !== null) {
        setStatusMessage(refreshWarning);
      }
      setRecordingState(initialRecordingState);
    } catch (error) {
      setRecordingState({
        ...initialRecordingState,
        errorMessage: error instanceof Error ? error.message : "未知录音保存错误"
      });
    }
  }

  /**
   * 保存说话人配置后同步普通设置版本；失败只生成提醒，不改变已保存结论。
   *
   * Returns:
   *   刷新失败提示；刷新成功或没有刷新回调时返回 null。
   */
  async function refreshSettingsSnapshotAfterMutation(
    candidateSettings?: ConsoleSettings
  ): Promise<string | null> {
    try {
      const refreshedSettings = await onSettingsSnapshotRefresh?.(candidateSettings);
      return refreshedSettings === null
        ? formatSavedRefreshWarning(null, "设置快照暂时不可用")
        : null;
    } catch (refreshError) {
      return formatSavedRefreshWarning(refreshError, "设置快照暂时不可用");
    }
  }

  function stopRecordingResources(): void {
    // 先失效当前录音，再关闭连接，避免主动关闭产生的回调误报为录音失败。
    recordingGenerationRef.current += 1;
    recordingInProgressRef.current = false;
    processorRef.current?.disconnect();
    sourceRef.current?.disconnect();
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop());
    if (websocketRef.current !== null && websocketRef.current.readyState <= WebSocket.OPEN) {
      websocketRef.current.close();
    }
    void audioContextRef.current?.close();
    processorRef.current = null;
    sourceRef.current = null;
    mediaStreamRef.current = null;
    audioContextRef.current = null;
    websocketRef.current = null;
    replacingSampleIdRef.current = null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>说话人确认</CardTitle>
        <CardDescription>
          录制自己的声音作为唯一白名单；开启后实时语音会只放行通过白名单确认的说话人。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {errorMessage !== null ? (
          <div className="flex items-center justify-between gap-3" role="alert">
            <p className="text-sm font-medium text-destructive">说话人确认异常：{errorMessage}</p>
            {profile === null ? <Button onClick={() => setLoadAttempt((current) => current + 1)} size="sm" type="button" variant="outline">重试</Button> : null}
          </div>
        ) : null}
        {recordingState.errorMessage !== null ? (
          <p className="text-sm font-medium text-destructive" role="alert">录音失败：{recordingState.errorMessage}</p>
        ) : null}
        {statusMessage !== null ? (
          <p className="text-sm text-muted-foreground" role="status">{statusMessage}</p>
        ) : null}

        {profile === null ? (
          errorMessage === null ? <p className="py-2 text-sm text-muted-foreground" role="status">
            正在读取说话人确认状态。
          </p> : null
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
              <div className="min-w-0 max-w-xl space-y-1">
                <h3 className="text-sm font-medium">只回应我的声音</h3>
                <p className="text-sm leading-relaxed text-muted-foreground">
                  {profile.can_enable ? "白名单录音已就绪，可以开启确认。" : "至少录制 1 段声音后才能开启。"}
                </p>
                <div className="flex flex-wrap gap-2 pt-1">
                  <Badge variant={profile.enabled ? "success" : "secondary"}>
                    {profile.enabled ? "已开启" : "未开启"}
                  </Badge>
                  <Badge variant="secondary">
                    已录制 {profile.samples.length} / {profile.max_sample_count} 段
                  </Badge>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-muted-foreground">{profile.enabled ? "开启" : "关闭"}</span>
                  <Switch
                    aria-label={profile.enabled ? "关闭说话人确认" : "开启说话人确认"}
                    checked={profile.enabled}
                    disabled={!canToggle}
                    onCheckedChange={(checked) => void handleToggle(checked)}
                  />
                </div>
                <Button
                  disabled={!canRecord || isBusy}
                  onClick={() => void handleStartRecording()}
                  type="button"
                >
                  {profile.samples.length >= profile.max_sample_count ? "录音已满" : "开始录制"}
                </Button>
                <Button
                  aria-expanded={isSampleListExpanded}
                  onClick={() => setIsSampleListExpanded((current) => !current)}
                  type="button"
                  variant="outline"
                >
                  {isSampleListExpanded ? "收起说话人样本" : "管理说话人样本"}
                </Button>
              </div>
            </div>

            {recordingState.status !== "idle" ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-primary/30 bg-accent px-4 py-2.5">
                <p className="text-sm font-medium text-accent-foreground" role="status">
                  {recordingState.status === "saving" ? "正在保存录音…" : `请自然说话 · 有效人声 ${recordingState.speechSeconds.toFixed(1)} / ${profile.required_speech_seconds.toFixed(1)} 秒`}
                </p>
                {recordingState.status === "recording" ? (
                  <Button onClick={() => {
                    stopRecordingResources();
                    setRecordingState(initialRecordingState);
                  }} size="sm" type="button" variant="outline">取消录音</Button>
                ) : null}
              </div>
            ) : null}
            {pendingWrite !== null ? <p className="text-xs text-muted-foreground" role="status">{pendingWrite === "toggle" ? "正在保存说话人设置…" : "正在删除录音…"}</p> : null}

            <p className="rounded-lg border bg-muted/50 px-4 py-3 text-sm text-muted-foreground">
              建议录制自然说、稍轻声和稍大声等不同音量片段，白名单会更稳定。
            </p>

            {isSampleListExpanded ? (
              <div className="space-y-3">
                {profile.samples.length === 0 ? (
                  <EmptyState>还没有白名单录音，先点击开始录制。</EmptyState>
                ) : (
                  profile.samples.map((sample, index) => (
                    <article
                      className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 rounded-lg border p-3"
                      key={sample.id}
                    >
                      <div className="min-w-0 space-y-0.5">
                        <h3 className="text-sm font-medium">片段 {index + 1}</h3>
                        <p className="truncate text-xs text-muted-foreground">{sample.id}</p>
                      </div>
                      <audio aria-label={`白名单片段 ${index + 1} 音频`} className="h-9 max-w-full" controls src={sample.audio_url} />
                      <div className="flex gap-2">
                        <Button
                          disabled={isBusy}
                          onClick={() => void handleStartRecording(sample.id)}
                          size="sm"
                          type="button"
                          variant="outline"
                        >
                          替换
                        </Button>
                        <Button
                          aria-label={`删除 ${sample.id}`}
                          disabled={isBusy}
                          onClick={() => void handleDelete(sample.id)}
                          size="sm"
                          type="button"
                          variant="outline"
                        >
                          删除
                        </Button>
                      </div>
                    </article>
                  ))
                )}
              </div>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function buildSpeakerRecordingWebSocketUrl(baseUrl: string): string {
  const url = new URL(baseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/api/speaker-verification/recording-stream";
  url.search = "";
  return url.toString();
}
