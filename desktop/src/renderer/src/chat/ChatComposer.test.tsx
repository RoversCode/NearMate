import { act } from "react";
import ReactDOM from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatComposer } from "./ChatComposer";

describe("ChatComposer", () => {
  let container: HTMLDivElement;
  let root: ReactDOM.Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.appendChild(container);
    root = ReactDOM.createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
  });

  it("输出不可用时保留草稿编辑能力但暂停发送", async () => {
    const onSubmit = vi.fn(async () => undefined);
    await act(async () => {
      root.render(
        <ChatComposer
          errorMessage="助手输出暂时不可用"
          isSubmissionDisabled
          onSubmit={onSubmit}
        />
      );
    });
    const textarea = container.querySelector("textarea")!;

    act(() => {
      textarea.value = "稍后继续发送";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(textarea.value).toBe("稍后继续发送");
    expect(textarea.disabled).toBe(false);
    expect(container.querySelector("button")!.hasAttribute("disabled")).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "助手输出暂时不可用"
    );
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("输入框聚焦时触发非阻塞环境预采集", async () => {
    const onFocus = vi.fn();
    await act(async () => {
      root.render(
        <ChatComposer
          errorMessage={null}
          onFocus={onFocus}
          onSubmit={vi.fn()}
        />
      );
    });

    await act(async () => container.querySelector("textarea")!.focus());

    expect(onFocus).toHaveBeenCalledOnce();
  });

  it("输入区只展示文件材料入口", async () => {
    await act(async () => {
      root.render(<ChatComposer errorMessage={null} onSelectFiles={vi.fn()} onSubmit={vi.fn()} />);
    });

    expect(container.querySelectorAll(".chat-composer-icon-button")).toHaveLength(1);
    expect(container.querySelector('[aria-label="添加截图"]')).toBeNull();
    expect(container.querySelector('[aria-label="语音输入由桌宠接管"]')).toBeNull();
  });

  it("短文本粘贴作为普通正文提交", async () => {
    const onSubmit = vi.fn(async () => undefined);
    await act(async () => {
      root.render(
        <ChatComposer
          errorMessage={null}
          onPasteText={async () => "inline"}
          onSubmit={onSubmit}
        />
      );
    });
    const textarea = container.querySelector("textarea")!;

    // jsdom 无法在 ClipboardEvent 构造时写入文本，直接注入只读 clipboardData。
    const pasteEvent = new Event("paste", { bubbles: true, cancelable: true }) as ClipboardEvent;
    Object.defineProperty(pasteEvent, "clipboardData", { value: { getData: () => "引用 https://example.com" } });
    await act(async () => textarea.dispatchEvent(pasteEvent));

    expect(textarea.value).toBe("引用 https://example.com");
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(onSubmit).toHaveBeenCalledWith({
      messageText: "引用 https://example.com"
    });
  });

  it("长文本粘贴转为材料后不写入输入框", async () => {
    await act(async () => {
      root.render(
        <ChatComposer
          errorMessage={null}
          onPasteText={async () => "material"}
          onSubmit={vi.fn()}
        />
      );
    });
    const textarea = container.querySelector("textarea")!;
    const pasteEvent = new Event("paste", { bubbles: true, cancelable: true }) as ClipboardEvent;
    Object.defineProperty(pasteEvent, "clipboardData", { value: { getData: () => "很长的文本" } });

    await act(async () => textarea.dispatchEvent(pasteEvent));

    expect(textarea.value).toBe("");
    expect(container.querySelector("button")!.hasAttribute("disabled")).toBe(true);
  });

  it("分类期间同步锁定重复粘贴和发送，并按冻结选区插入短文本", async () => {
    const classification = createDeferred<"inline" | "material">();
    const onPasteText = vi.fn(() => classification.promise);
    const onSubmit = vi.fn(async () => undefined);
    await act(async () => {
      root.render(
        <ChatComposer
          errorMessage={null}
          onPasteText={onPasteText}
          onSubmit={onSubmit}
        />
      );
    });
    const textarea = container.querySelector("textarea")!;
    act(() => {
      textarea.value = "前后";
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      textarea.setSelectionRange(1, 1);
    });
    const pasteEvent = makePasteEvent("插入");
    const duplicatePasteEvent = makePasteEvent("重复");

    act(() => {
      textarea.dispatchEvent(pasteEvent);
      textarea.dispatchEvent(duplicatePasteEvent);
      container.querySelector("form")!.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true })
      );
    });

    expect(onPasteText).toHaveBeenCalledOnce();
    expect(textarea.readOnly).toBe(true);
    expect(container.querySelector("button")!.hasAttribute("disabled")).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();

    await act(async () => {
      classification.resolve("inline");
      await classification.promise;
      await Promise.resolve();
    });

    expect(textarea.value).toBe("前插入后");
    expect(textarea.selectionStart).toBe(3);
    expect(textarea.readOnly).toBe(false);
  });

  it("文件按钮把选中的文件交给材料入口", async () => {
    const onSelectFiles = vi.fn(async (_files: File[]) => undefined);
    await act(async () => {
      root.render(
        <ChatComposer
          errorMessage={null}
          onSelectFiles={onSelectFiles}
          onSubmit={vi.fn()}
        />
      );
    });

    const file = new File(["内容"], "说明.txt", { type: "text/plain" });
    const input = container.querySelector<HTMLInputElement>(".chat-composer-file-input")!;
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    await act(async () => {
      input.dispatchEvent(new Event("change", { bubbles: true }));
      await Promise.resolve();
    });

    expect(onSelectFiles).toHaveBeenCalledWith([file]);
  });
});

function makePasteEvent(text: string): ClipboardEvent {
  const event = new Event("paste", { bubbles: true, cancelable: true }) as ClipboardEvent;
  Object.defineProperty(event, "clipboardData", { value: { getData: () => text } });
  return event;
}

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(nextResolve => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}
// @vitest-environment jsdom
