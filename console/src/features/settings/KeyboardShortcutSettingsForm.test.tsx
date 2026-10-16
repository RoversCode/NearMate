import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KeyboardShortcutSettingsForm } from "./KeyboardShortcutSettingsForm";

function renderShortcut() {
  const save = vi.fn(async () => undefined);
  render(<KeyboardShortcutSettingsForm title="追问快捷键" description="切换跟进并停止讲话" shortcut="CommandOrControl+Alt+Space"
    enabled isSaving={false} toggleAriaLabel="关闭追问快捷键" recordButtonLabel="录入快捷键" statusLabel="可用"
    statusMessage="快捷键可用" onToggle={vi.fn(async () => undefined)} onShortcutRecorded={save} />);
  fireEvent.click(screen.getByRole("button", { name: "录入快捷键" }));
  return save;
}

describe("快捷键录入", () => {
  it("只按字母不保存，组合键也需要用户确认", async () => {
    const save = renderShortcut();
    const input = screen.getByLabelText("快捷键录入区");
    fireEvent.keyDown(input, { key: "f", code: "KeyF" });
    expect(screen.getByRole("alert").textContent).toContain("搭配 Ctrl");
    expect(save).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "f", code: "KeyF", ctrlKey: true, altKey: true });
    expect(save).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "使用此快捷键" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("CommandOrControl+Alt+F"));
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("取消录入不保存，并把焦点还给原操作按钮", async () => {
    const save = renderShortcut();
    fireEvent.keyDown(screen.getByLabelText("快捷键录入区"), { key: "q", code: "KeyQ", ctrlKey: true });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(save).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "录入快捷键" }));
  });
});
