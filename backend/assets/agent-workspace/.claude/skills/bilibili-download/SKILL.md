---
name: bilibili-download
description: 使用 BBDown 下载 B 站（bilibili）视频或音频到任务交付目录，支持 BV 号、av 号、b23 短链和番剧链接，可选清晰度、分P、弹幕和纯音频。当用户要求下载、保存、抓取 B 站视频/番剧/音频时使用。
---

# B 站视频下载（BBDown）

当用户给出 B 站链接（BV 号、av 号、b23 短链、番剧 ss/ep 链接）并要求下载或保存时使用本 Skill。底层工具是开源下载器 [BBDown](https://github.com/nilaoda/BBDown)，仅供个人学习研究，下载内容的合规性由使用者自行负责。

## 使用方法

1. 确认用户给出的链接与下载诉求（整视频还是纯音频、清晰度、分P、是否要弹幕）。
2. 调用本 Skill 的 `scripts/download_bilibili.py`，输出目录必须指向本次任务说明指定的交付目录。
3. 命令成功后从 stdout 最后一行的 JSON 里取得产出文件清单，确认文件真实存在且非空，再在最终回复中列出绝对路径。

```powershell
python "${CLAUDE_SKILL_DIR}/scripts/download_bilibili.py" `
  --url "<B站链接或BV号>" `
  --output-dir "<任务交付目录>"
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `--url` | 必填。视频地址或 av/bv/BV/ep/ss 号。 |
| `--output-dir` | 必填。交付目录绝对路径。 |
| `--quality` | 清晰度上限：`best`（默认，取可用最高）/ `1080p` / `720p` / `480p` / `360p`。 |
| `--audio-only` | 只下载音频（.m4a）。做语音转写、内容摘要时优先用它，速度快、体积小。 |
| `--pages` | 分P 选择：`3` / `1,2,10` / `1-10` / `ALL` / `LAST`。多P 视频必须显式指定，否则可能解析范围不符合预期。 |
| `--danmaku` | 同时下载弹幕文件（xml/ass）。 |
| `--cookie-file` | 包含 B 站 cookie（`SESSDATA=...`）的文本文件路径，用于解锁登录清晰度与会员内容。 |

## 常见问题

- 未登录时 B 站通常只给到低清晰度（如 360P/480P）。用户要求高清晰度但没有提供 cookie 时，说明这个限制并询问是否提供 `--cookie-file`，不要伪造下载成功。
- BBDown 不存在时会自动按钉死版本下载并校验 SHA256，缓存供后续任务复用；下载失败就明确报缺少工具，不要伪造结果。
- 混流依赖系统已安装的 ffmpeg；如果失败信息指向混流，检查 ffmpeg 是否在 PATH。
- 下载失败（解析失败、地区限制、会员专享）时如实汇报 BBDown 的失败原因，不要重试刷屏。
