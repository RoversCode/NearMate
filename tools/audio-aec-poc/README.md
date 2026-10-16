# Windows AEC PoC

这个工具用于验证：电脑扬声器播放出来的声音被麦克风收进去后，能否通过 WASAPI loopback reference + LiveKit APM AEC 从麦克风流里压下去。

## 运行

```powershell
.venv\Scripts\python.exe -m audio_aec_poc.main --duration-seconds 10
```

默认会使用 `48000 Hz` 作为 AEC 内部采样率，`mic_clean.wav` 默认保存为 `16000 Hz`，并在正式写入 WAV 前预热 AEC `2` 秒。预热阶段只把麦克风和扬声器参考送进 AEC，不写入 `mic_raw.wav`、`speaker_ref.wav`、`mic_clean.wav`，用于让 AEC 先适应当前设备和房间。

如果要对比冷启动效果，可以关闭预热：

```powershell
.venv\Scripts\python.exe -m audio_aec_poc.main --duration-seconds 10 --warmup-seconds 0
```

如果要复现旧版 16k 行为，可以显式指定：

```powershell
.venv\Scripts\python.exe -m audio_aec_poc.main --duration-seconds 10 --sample-rate 16000 --clean-output-sample-rate 16000 --warmup-seconds 0
```

如果要测试“48k AEC + 16k clean 输出”，可以显式指定：

```powershell
.venv\Scripts\python.exe -m audio_aec_poc.main --duration-seconds 10 --sample-rate 48000 --clean-output-sample-rate 16000
```

如果缺少 `soundcard`、`soxr` 等运行依赖，请在仓库根目录同步统一 Python 环境，不要为本 PoC 创建单独的虚拟环境：

```powershell
pnpm python:install
```

## LiveKit sidecar mode

桌面应用进入 LiveKit 实时房间时，可以启动音频 sidecar 作为独立 participant。协议 `0` 本地会话采集默认麦克风和扬声器 loopback reference，经过 LiveKit APM AEC 后发布名为 `clean-mic` 的本地音频轨；宿主进程退出后，sidecar 会通过 `--parent-pid` 自动停止。

sidecar 只在 WTS 协议 `2` 会话中启用 RDP 连续采集：麦克风由 Windows `MediaCapture` 写入临时 WAV，后台 worker 增量读取，先用单个持久的固定比例 SoXR 流转换到 `clean-mic` 发布采样率，再用单个持久的 `audiotsm==0.1.2` WSOLA 流按 0.85 到 1.15 的范围改变时长但保持音高，最后放入固定容量 ring buffer。时钟控制器用滑动窗口估算 RDP 样本实际到达速率，并以约 2 秒缓冲目标做小幅闭环调速；到达时刻不会改变样本顺序，也不会改变 SoXR 比例。RDP 不打开扬声器 loopback，也不使用 AEC，`clean-mic` 直接发布时钟恢复后的原始麦克风。设备通过 soundcard endpoint GUID 映射到 WinRT 音频采集设备，启动前积累约 2 秒音频以吸收 RDP 交付抖动，最大积压约 5 秒。协议 `0`、协议 `1`、未知协议和 WTS 查询失败都保留原有低延迟 AEC。RDP 需要安装项目锁定的 Windows Runtime Python 依赖，不能用 DirectShow 或 PortAudio 作为 fallback。

```powershell
.venv\Scripts\python.exe -m audio_aec_sidecar.main `
  --livekit-url "wss://livekit.example" `
  --token "<desktop-aec-token>" `
  --room-name "desktop-room" `
  --identity "desktop-aec-worker" `
  --parent-pid $PID `
  --sample-rate 48000 `
  --clean-output-sample-rate 16000 `
  --warmup-seconds 2 `
  --debug-output-dir "tools/audio-aec-poc/output/sidecar-debug"
```

`--debug-output-dir` 是可选参数。启用后会在指定目录下为每次运行创建一个 UTC 时间戳子目录，并在其中写出 `mic_raw.wav`、`speaker_ref.wav`、`mic_clean.wav` 和 `diagnostics.json`。本地模式下三路 WAV 分别对应 AEC 前麦克风、扬声器参考和实际发布前的 clean 麦克风；RDP 模式下三路都使用发布采样率，`speaker_ref.wav` 是零参考，`mic_raw.wav` 与 `mic_clean.wav` 都是未经过 AEC、但已经经过固定 SoXR 与 WSOLA 时钟恢复的原始麦克风。`diagnostics.json` 记录本轮开始/结束时间、发布帧数、输入速率、ring buffer 水位、WSOLA 速度和失败原因。调试文件可能包含本地语音内容，只应写入本机开发目录，不要提交到版本库。

## 输出

每次运行会生成：

```text
tools/audio-aec-poc/output/<run-id>/
  mic_raw.wav
  speaker_ref.wav
  mic_clean.wav
  diagnostics.json
  manifest.json
```

- `mic_raw.wav`：原始麦克风输入。
- `speaker_ref.wav`：系统扬声器 loopback reference。
- `mic_clean.wav`：LiveKit APM AEC 处理后的麦克风输入，可通过 `--clean-output-sample-rate` 单独指定保存采样率。
- `manifest.json`：设备名、AEC 内部采样率、clean 输出采样率、正式运行时长、预热时长和处理统计。

## 本地 AEC 验收

1. 启动工具。
2. 电脑扬声器播放拼音或伴侣 TTS。
3. 人不说话。
4. 停止录制。
5. 听三份 WAV。

通过标准：

```text
mic_raw.wav 能听到扬声器声音。
speaker_ref.wav 能听到扬声器声音。
mic_clean.wav 里的扬声器声音明显变小。
```

## RDP 连续采集验收

1. 通过 Mac Windows App 建立 RDP 连接，并确认 RDP 麦克风重定向已开启。
2. 启动桌面应用或 sidecar。
3. 说连续句子，不需要播放助手语音。
4. 停止运行，听 `mic_raw.wav` 和 `mic_clean.wav`。

通过标准：

```text
用户声音连续，没有周期性一秒有声、半秒静音。
mic_clean.wav 与 mic_raw.wav 的有效语音长度基本一致。
LiveKit 后端出现 VAD/ASR 记录。
```
