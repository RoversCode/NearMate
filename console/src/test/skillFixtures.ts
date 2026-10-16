import type {
  Skill,
  SkillDetail,
  SkillLibraryResponse,
  SkillUpdateResponse
} from "../api/contracts/skills";

/** 构造一个依赖就绪且默认开启的媒体处理 Skill。 */
export function createReadySkill(overrides: Partial<Skill> = {}): Skill {
  return {
    id: "ffmpeg-convert",
    name: "FFmpeg 格式转换",
    summary: "转换音频或视频的格式、编码、码率、分辨率和封装格式。",
    category: "媒体处理",
    version: "1.0.0",
    default_enabled: true,
    source: "builtin",
    trigger_scenarios: ["转换媒体格式", "调整编码或码率"],
    inputs: ["音频或视频文件", "目标格式或编码要求"],
    outputs: ["转换后的媒体文件"],
    dependencies: [
      {
        kind: "executable",
        name: "ffmpeg"
      }
    ],
    enabled: true,
    ready: true,
    usable: true,
    unavailable_reason: null,
    dependency_status: [
      {
        kind: "executable",
        name: "ffmpeg",
        ready: true,
        path: "C:/tools/ffmpeg.exe",
        detected_version: "7.1.0",
        reason: null
      }
    ],
    ...overrides
  };
}

/** 构造一个因为外部命令缺失而无法开启的图片文字提取 Skill。 */
export function createUnavailableSkill(overrides: Partial<Skill> = {}): Skill {
  return {
    id: "image-ocr",
    name: "图片文字提取",
    summary: "从本地图片中识别并整理文字内容。",
    category: "文档处理",
    version: "1.0.0",
    default_enabled: false,
    source: "builtin",
    trigger_scenarios: ["识别图片中的文字"],
    inputs: ["本地图片文件"],
    outputs: ["识别并整理后的文字"],
    dependencies: [
      {
        kind: "executable",
        name: "tesseract",
        minimum_version: "5.0.0"
      }
    ],
    enabled: false,
    ready: false,
    usable: false,
    unavailable_reason: "系统找不到命令 tesseract",
    dependency_status: [
      {
        kind: "executable",
        name: "tesseract",
        minimum_version: "5.0.0",
        ready: false,
        path: null,
        detected_version: null,
        reason: "系统找不到命令 tesseract"
      }
    ],
    ...overrides
  };
}

/** 构造技能库列表接口的完整、内部自洽快照。 */
export function createSkillLibraryResponse(
  overrides: Partial<SkillLibraryResponse> = {}
): SkillLibraryResponse {
  const skills = overrides.skills ?? [createReadySkill(), createUnavailableSkill()];
  return {
    skills,
    revision: overrides.revision ?? 10,
    issues: overrides.issues ?? [],
    summary: overrides.summary ?? {
      total: skills.length,
      enabled: skills.filter(skill => skill.enabled).length,
      ready: skills.filter(skill => skill.ready).length,
      usable: skills.filter(skill => skill.usable).length
    }
  };
}

/** 构造 Skill 详情接口返回的纯文本说明和相对文件清单。 */
export function createSkillDetail(overrides: Partial<SkillDetail> = {}): SkillDetail {
  return {
    ...createReadySkill(),
    instruction: "# FFmpeg 格式转换\n\n<img src=x onerror=alert(1)>\n\n只把结果写入交付目录。",
    files: ["SKILL.md", "manifest.json", "scripts/convert_media.py"],
    revision: 10,
    ...overrides
  };
}

/** 构造 Skill 开关修改后的版本化响应。 */
export function createSkillUpdateResponse(
  enabled: boolean,
  overrides: Partial<SkillUpdateResponse> = {}
): SkillUpdateResponse {
  return {
    skill: createReadySkill({
      enabled,
      usable: enabled
    }),
    revision: 11,
    ...overrides
  };
}
