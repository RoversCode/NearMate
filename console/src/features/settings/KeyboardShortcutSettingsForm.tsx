import { useEffect, useRef, useState } from "react";
import { Keyboard, Pencil } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

type KeyboardShortcutSettingsFormProps = {
  title: string;
  description: string;
  shortcut: string;
  enabled: boolean;
  isSaving: boolean;
  toggleAriaLabel: string;
  recordButtonLabel: string;
  statusLabel: string;
  statusMessage: string;
  saveErrorMessage?: string | null;
  onToggle: () => Promise<void>;
  onShortcutRecorded: (accelerator: string) => Promise<void | boolean>;
};

/**
 * 用一行展示快捷键，在有焦点约束的弹窗内录入新组合键。
 *
 * Args:
 *   props: 当前快捷键、可用状态和保存动作。
 *
 * Returns:
 *   紧凑设置行和录入弹窗；确认后才保存，Escape 或取消不会改变原快捷键。
 */
export function KeyboardShortcutSettingsForm({
  title, description, shortcut, enabled, isSaving, toggleAriaLabel,
  recordButtonLabel, statusLabel, statusMessage, onToggle, onShortcutRecorded, saveErrorMessage
}: KeyboardShortcutSettingsFormProps): JSX.Element {
  const [isRecording, setIsRecording] = useState(false);
  const [candidate, setCandidate] = useState<string | null>(null);
  const [recordingError, setRecordingError] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const captureRef = useRef<HTMLDivElement>(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    if (!isRecording) return;

    function handleKeyDown(event: KeyboardEvent): void {
      // Tab 和 Escape 留给弹窗的焦点导航与取消，录入时不拦截输入法组合事件。
      if (event.key === "Escape" || event.key === "Tab" || event.isComposing || event.repeat || isSaving) return;
      const accelerator = keyboardEventToElectronAccelerator(event);
      if (accelerator === null) return;
      // Enter 仍可激活“使用此快捷键”，只有输入区中的键才用于录入。
      if (event.target !== captureRef.current) return;
      event.preventDefault();
      event.stopPropagation();
      if (!(event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)) {
        setRecordingError("请搭配 Ctrl、Alt 或 Shift，避免影响日常输入。");
        return;
      }
      setCandidate(accelerator);
      setRecordingError(null);
    }

    window.addEventListener("keydown", handleKeyDown, true);
    return () => window.removeEventListener("keydown", handleKeyDown, true);
  }, [isRecording, isSaving]);

  async function saveShortcut(): Promise<void> {
    if (candidate === null || isSaving || submittingRef.current) return;
    submittingRef.current = true;
    try {
      const succeeded = await onShortcutRecorded(candidate);
      if (succeeded === false) {
        setRecordingError("快捷键保存失败，请重试。");
        return;
      }
      setIsRecording(false);
    } catch (error) {
      setRecordingError(error instanceof Error ? error.message : "快捷键保存失败，请重试。");
    } finally {
      submittingRef.current = false;
    }
  }

  return (
    <div className="shortcut-setting-row">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium">{title}</h3>
          <Badge className="px-1.5 py-0 text-[10px] font-medium" title={statusMessage} variant={shortcutStatusBadgeVariant(statusLabel)}>{statusLabel}</Badge>
        </div>
        <p className="max-w-xl text-xs leading-relaxed text-muted-foreground">{description}</p>
        {statusLabel !== "可用" && statusLabel !== "已关闭" ? <p className="text-xs text-muted-foreground">{statusMessage}</p> : null}
      </div>
      <div className="flex items-center justify-between gap-4 sm:justify-end">
        <Button
          aria-label={recordButtonLabel}
          className="group h-9 gap-1.5 px-2.5 font-normal"
          disabled={isSaving}
          onClick={() => { setCandidate(null); setRecordingError(null); setIsRecording(true); }}
          ref={triggerRef}
          title={`修改${title}`}
          type="button"
          variant="outline"
        >
          <ShortcutKeys shortcut={shortcut} />
          <Pencil aria-hidden="true" className="ml-1 size-3 text-muted-foreground group-hover:text-primary" />
        </Button>
        <Switch aria-label={toggleAriaLabel} checked={enabled} disabled={isSaving} onCheckedChange={() => void onToggle()} />
      </div>
      <Dialog open={isRecording} onOpenChange={open => { if (!submittingRef.current) setIsRecording(open); }}>
        <DialogContent
          onOpenAutoFocus={(event) => { event.preventDefault(); captureRef.current?.focus(); }}
          onCloseAutoFocus={(event) => { event.preventDefault(); triggerRef.current?.focus(); }}
        >
          <DialogHeader>
            <DialogTitle>修改{title}</DialogTitle>
            <DialogDescription>按下 Ctrl、Alt 或 Shift 与一个普通键的组合。按 Escape 取消。</DialogDescription>
          </DialogHeader>
          <div
            aria-label="快捷键录入区"
            className="flex min-h-28 flex-col items-center justify-center gap-4 rounded-lg border border-dashed bg-muted/40 p-5 outline-none focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-ring/20"
            ref={captureRef}
            tabIndex={0}
          >
            <Keyboard aria-hidden="true" className="size-6 text-muted-foreground" />
            <div aria-live="polite" className="flex gap-2">{candidate ? <ShortcutKeys shortcut={candidate} /> : "请按下新的组合键"}</div>
          </div>
          {recordingError ? <p className="text-sm text-destructive" role="alert">{saveErrorMessage || recordingError}</p> : null}
          <DialogFooter>
            <Button disabled={isSaving} onClick={() => { if (!submittingRef.current) setIsRecording(false); }} type="button" variant="outline">取消</Button>
            <Button disabled={!candidate || isSaving} onClick={() => void saveShortcut()} type="button">{isSaving ? "保存中…" : "使用此快捷键"}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/**
 * 将内部 accelerator 转成 Windows 用户熟悉的键帽。
 *
 * Args:
 *   shortcut: Electron 组合键字符串。
 *
 * Returns:
 *   可读的按键列表，不改变实际注册值。
 */
function ShortcutKeys({ shortcut }: { shortcut: string }): JSX.Element {
  const labels: Record<string, string> = { CommandOrControl: "Ctrl", Control: "Ctrl", Space: "空格", Up: "↑", Down: "↓", Left: "←", Right: "→" };
  return <>{shortcut.split("+").map((key, index) => <kbd className="shortcut-key" key={`${key}-${index}`}>{labels[key] ?? key}</kbd>)}</>;
}

/**
 * 把快捷键注册状态文案映射为徽标语义档位。
 *
 * Args:
 *   statusLabel: 快捷键状态文案（可用/待检测/已关闭/冲突等）。
 *
 * Returns:
 *   对应的徽标 variant。
 */
function shortcutStatusBadgeVariant(statusLabel: string): "success" | "outline" | "secondary" | "destructive" {
  if (statusLabel === "可用") {
    return "success";
  }
  if (statusLabel === "待检测") {
    return "outline";
  }
  if (statusLabel === "已关闭") {
    return "secondary";
  }
  return "destructive";
}

/**
 * 把浏览器键盘事件转换为 Electron accelerator。
 *
 * Args:
 *   event: 录入模式捕获到的键盘事件。
 *
 * Returns:
 *   Electron 可注册的组合键字符串；只按修饰键时返回 null。
 */
function keyboardEventToElectronAccelerator(event: KeyboardEvent): string | null {
  const mainKey = normalizeAcceleratorMainKey(event);
  if (mainKey === null) {
    return null;
  }

  const parts: string[] = [];
  if (event.ctrlKey || event.metaKey) {
    parts.push("CommandOrControl");
  }
  if (event.altKey) {
    parts.push("Alt");
  }
  if (event.shiftKey) {
    parts.push("Shift");
  }
  parts.push(mainKey);
  return parts.join("+");
}

/**
 * 归一化快捷键中的主键。
 *
 * Args:
 *   event: 录入模式捕获到的键盘事件。
 *
 * Returns:
 *   Electron accelerator 的主键片段；修饰键本身返回 null。
 */
function normalizeAcceleratorMainKey(event: KeyboardEvent): string | null {
  if (["Control", "Meta", "Alt", "Shift"].includes(event.key)) {
    return null;
  }

  if (/^Key[A-Z]$/.test(event.code)) {
    return event.code.slice("Key".length);
  }
  if (/^Digit[0-9]$/.test(event.code)) {
    return event.code.slice("Digit".length);
  }
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(event.code)) {
    return event.code;
  }

  const keyNameByCode: Record<string, string> = {
    Space: "Space",
    ArrowUp: "Up",
    ArrowDown: "Down",
    ArrowLeft: "Left",
    ArrowRight: "Right",
    Escape: "Esc",
    Enter: "Enter",
    Tab: "Tab",
    Backspace: "Backspace",
    Delete: "Delete"
  };
  if (keyNameByCode[event.code]) {
    return keyNameByCode[event.code];
  }

  if (event.key === "+") return "Plus";
  return event.key.length === 1 ? event.key.toUpperCase() : null;
}
