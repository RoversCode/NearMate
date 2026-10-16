type CharacterVoiceCaptionTiming = {
  ms_per_unit: number;
  calibration_text: string;
  duration_ms: number;
  unit_count: number;
  measured_at: number;
};

/**
 * 提醒设置由前端侧边栏与后端控制层共同消费，因此单独抽成共享结构。
 * 现在这份设置对象也承载最近一次成功选择的人物模型目录名，供后续窗口间同步时复用同一份契约。
 * 角色名称已经收敛到角色卡，不再作为普通设置字段传播。
 */
export type ReminderSettings = {
  show_speech_bubbles: boolean;
  active_character_caption_timing?: CharacterVoiceCaptionTiming | null;
  followup_toggle_shortcut_enabled?: boolean;
  followup_toggle_shortcut?: string;
  avatar_visibility_shortcut_enabled?: boolean;
  avatar_visibility_shortcut?: string;
  selection_capture_shortcut_enabled?: boolean;
  selection_capture_shortcut?: string;
  selected_live2d_model_id: string;
};
