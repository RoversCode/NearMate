/** Console 的使用手册入口集中在这里，页面只选择当前任务对应的章节。 */
const GUIDE_ROOT = "https://roverscode.github.io/NearMate/guide/";

/** 产品帮助地址；首次初始化锚点与官网手册使用同一套步骤名称。 */
export const NEARMATE_HELP_LINKS = {
  manual: `${GUIDE_ROOT}index.html`,
  firstRun: {
    deepseek: `${GUIDE_ROOT}first-run.html#deepseek`,
    qwen: `${GUIDE_ROOT}first-run.html#qwen`,
    verifyAi: `${GUIDE_ROOT}first-run.html#verify-ai`,
    checkComputer: `${GUIDE_ROOT}first-run.html#check-computer`,
    voiceModel: `${GUIDE_ROOT}first-run.html#voice-model`,
    preview: `${GUIDE_ROOT}first-run.html#preview`
  },
  firstMessage: `${GUIDE_ROOT}chat-and-voice.html#first-message`,
  voice: `${GUIDE_ROOT}chat-and-voice.html#voice`,
  optionalServices: `${GUIDE_ROOT}optional-services.html`,
  troubleshooting: `${GUIDE_ROOT}troubleshooting.html`
} as const;
