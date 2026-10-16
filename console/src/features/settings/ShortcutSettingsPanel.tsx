import type { ConsoleSettings } from "../../api/contracts/settings";
import type { ShortcutId, ShortcutStatusItem, ShortcutStatusPayload } from "../../api/contracts/shortcutStatus";
import { KeyboardShortcutSettingsForm } from "./KeyboardShortcutSettingsForm";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type ShortcutSettingsPanelProps = {
  settings: ConsoleSettings;
  shortcutStatus: ShortcutStatusPayload | null;
  isSaving: boolean;
  saveErrorMessage?: string | null;
  onSave: (settings: ConsoleSettings) => Promise<void | boolean>;
};

const shortcutDefinitions = [
  { id: "followup_toggle", enabledKey: "followup_toggle_shortcut_enabled", acceleratorKey: "followup_toggle_shortcut", title: "追问快捷键", description: "切换免唤醒跟进，同时立即停止当前讲话。", recordLabel: "录入快捷键" },
  { id: "avatar_visibility", enabledKey: "avatar_visibility_shortcut_enabled", acceleratorKey: "avatar_visibility_shortcut", title: "桌宠召回快捷键", description: "召回并置顶虚拟形象，再按一次收起。", recordLabel: "录入桌宠召回快捷键" },
  { id: "selection_capture", enabledKey: "selection_capture_shortcut_enabled", acceleratorKey: "selection_capture_shortcut", title: "收集选中内容快捷键", description: "将选中的文字、图片或网页片段加入待提交材料。", recordLabel: "录入收集快捷键" }
] as const;

/**
 * 将全局快捷键放在同一列表中，统一展示注册状态和修改入口。
 *
 * Args:
 *   props: 当前设置、桌面端注册结果、保存状态和保存回调。
 *
 * Returns:
 *   三行快捷键；修改仅更新目标字段，保持设置版本校验与后端权威状态。
 */
export function ShortcutSettingsPanel({ settings, shortcutStatus, isSaving, onSave, saveErrorMessage }: ShortcutSettingsPanelProps): JSX.Element {
  const shortcutStatusById = resolveShortcutStatusById(settings, shortcutStatus);
  return (
    <Card>
      <CardHeader>
        <CardTitle>全局快捷键</CardTitle>
        <CardDescription>在其他应用中也可使用。点击组合键修改，开关即时保存。</CardDescription>
      </CardHeader>
      <CardContent className="divide-y">
        {shortcutDefinitions.map(shortcut => (
          <KeyboardShortcutSettingsForm
            key={shortcut.id}
            title={shortcut.title}
            description={shortcut.description}
            shortcut={settings[shortcut.acceleratorKey]}
            enabled={settings[shortcut.enabledKey]}
            isSaving={isSaving}
            saveErrorMessage={saveErrorMessage}
            statusLabel={shortcutStatusById[shortcut.id].label}
            statusMessage={shortcutStatusById[shortcut.id].message}
            recordButtonLabel={shortcut.recordLabel}
            toggleAriaLabel={`${settings[shortcut.enabledKey] ? "关闭" : "开启"}${shortcut.title}`}
            onToggle={async () => {
              if (isSaving) return;
              await onSave({ ...settings, [shortcut.enabledKey]: !settings[shortcut.enabledKey] });
            }}
            onShortcutRecorded={accelerator => onSave({
              ...settings,
              [shortcut.acceleratorKey]: accelerator,
              [shortcut.enabledKey]: true
            })}
          />
        ))}
      </CardContent>
    </Card>
  );
}

type ShortcutStatusView = {
  label: string;
  message: string;
};

type ShortcutStatusViewById = Record<ShortcutId, ShortcutStatusView>;

/**
 * 合成设置页展示用的快捷键状态。
 *
 * Args:
 *   settings: 当前控制台设置快照。
 *   shortcutStatus: desktop main 上报的运行期注册状态。
 *
 * Returns:
 *   三类快捷键对应的状态徽标和说明。
 */
function resolveShortcutStatusById(
  settings: ConsoleSettings,
  shortcutStatus: ShortcutStatusPayload | null
): ShortcutStatusViewById {
  const localShortcuts = shortcutDescriptors(settings);
  const duplicatedAccelerators = duplicatedEnabledAccelerators(localShortcuts);
  const reportedStatusById = new Map<ShortcutId, ShortcutStatusItem>(
    shortcutStatus?.items.map((item) => [item.shortcut_id, item]) ?? []
  );

  return localShortcuts.reduce<ShortcutStatusViewById>((result, shortcut) => {
    if (shortcut.enabled && duplicatedAccelerators.has(normalizeAcceleratorKey(shortcut.accelerator))) {
      result[shortcut.id] = {
        label: "NearMate 内部冲突",
        message: "与 NearMate 其它快捷键重复"
      };
      return result;
    }

    const reported = reportedStatusById.get(shortcut.id);
    // 修改组合键后，旧组合键的“可用”不能冒充新组合键的注册结果。
    // 状态上报和设置快照走不同的传输链；即使组合键文字没变，旧 revision
    // 的“可用”也不能代表当前 revision 已经重新完成注册检查。
    const matchingStatus = reported
      && reported.settings_revision === settings.settings_revision
      && normalizeAcceleratorKey(reported.accelerator) === normalizeAcceleratorKey(shortcut.accelerator)
      && reported.enabled === shortcut.enabled ? reported : undefined;
    result[shortcut.id] = statusViewFromReportedItem(matchingStatus, shortcut.enabled);
    return result;
  }, {
    followup_toggle: { label: "待检测", message: "等待桌面端检测" },
    avatar_visibility: { label: "待检测", message: "等待桌面端检测" },
    selection_capture: { label: "待检测", message: "等待桌面端检测" }
  });
}

function shortcutDescriptors(settings: ConsoleSettings): Array<{ id: ShortcutId; enabled: boolean; accelerator: string }> {
  return [
    {
      id: "followup_toggle",
      enabled: settings.followup_toggle_shortcut_enabled,
      accelerator: settings.followup_toggle_shortcut
    },
    {
      id: "avatar_visibility",
      enabled: settings.avatar_visibility_shortcut_enabled,
      accelerator: settings.avatar_visibility_shortcut
    },
    {
      id: "selection_capture",
      enabled: settings.selection_capture_shortcut_enabled,
      accelerator: settings.selection_capture_shortcut
    }
  ];
}

function duplicatedEnabledAccelerators(
  shortcuts: Array<{ enabled: boolean; accelerator: string }>
): Set<string> {
  const counts = new Map<string, number>();
  for (const shortcut of shortcuts) {
    if (!shortcut.enabled) {
      continue;
    }
    const key = normalizeAcceleratorKey(shortcut.accelerator);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }

  return new Set(
    [...counts.entries()]
      .filter(([, count]) => count > 1)
      .map(([accelerator]) => accelerator)
  );
}

function normalizeAcceleratorKey(accelerator: string): string {
  return accelerator.trim().toLowerCase();
}

function statusViewFromReportedItem(
  item: ShortcutStatusItem | undefined,
  enabled: boolean
): ShortcutStatusView {
  if (!enabled) {
    return { label: "已关闭", message: "快捷键已关闭" };
  }
  if (item === undefined) {
    return { label: "待检测", message: "等待桌面端检测" };
  }
  if (item.status === "registered") {
    return { label: "可用", message: item.message || "快捷键可用" };
  }
  if (item.status === "disabled") {
    return { label: "已关闭", message: item.message || "快捷键已关闭" };
  }
  if (item.status === "internal_conflict") {
    return { label: "NearMate 内部冲突", message: item.message || "与 NearMate 其它快捷键重复" };
  }
  if (item.status === "unavailable") {
    return { label: "当前设备不可用", message: item.message || "可能被系统或其他应用占用" };
  }
  return { label: "待检测", message: item.message || "等待桌面端检测" };
}
