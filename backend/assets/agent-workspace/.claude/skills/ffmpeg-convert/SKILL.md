---
name: ffmpeg-convert
description: 使用 ffmpeg 对音频或视频文件做格式转换、封装转换、从视频提取音频（如视频转 mp3）、转码、调整码率或分辨率，并把结果写入当前任务的交付目录。当用户要求转换媒体格式、提取音频、压缩媒体或修改编码参数时使用。
---

# ffmpeg 格式转换

当用户要求转换音频或视频格式、编码、码率、分辨率或封装格式时使用本 Skill。

## 使用方法

1. 从任务材料清单中找到输入文件的绝对路径。
2. 根据用户要求确定输出文件名、封装格式和必要的 ffmpeg 参数。
3. 输出文件必须放到本次任务说明指定的交付目录。
4. 优先调用本 Skill 的 `scripts/convert_media.py`，不要使用字符串拼接执行 shell。
5. 命令成功后确认输出文件真实存在，再在最终回复中列出绝对路径。

## 命令示例

```powershell
  python "${CLAUDE_SKILL_DIR}/scripts/convert_media.py" `
  --input "<输入文件绝对路径>" `
  --output "<任务交付目录>/<输出文件名>"
```

需要额外编码参数时，在命令末尾使用 `--ffmpeg-arg`，每个参数单独提供一次：

```powershell
  python "${CLAUDE_SKILL_DIR}/scripts/convert_media.py" `
  --input "<输入文件>" `
  --output "<输出文件>" `
  --ffmpeg-arg=-vn `
  --ffmpeg-arg=-b:a `
  --ffmpeg-arg=192k
```

## 常见问题

- 如果系统找不到 ffmpeg，直接说明缺少运行依赖，不要伪造成功。
- 不要覆盖输入文件。
- 用户没有说明目标格式且无法从文件名判断时，调用 `AskUserQuestion` 询问。
- 转换后检查输出文件大小；空文件不能作为成功结果交付。
