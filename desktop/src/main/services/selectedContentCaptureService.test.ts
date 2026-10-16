import { describe, expect, it, vi } from "vitest";

import { createSelectedContentCaptureService } from "./selectedContentCaptureService";
import { createElectronSelectedContentClipboardPort } from "./selectionClipboardTransaction";

describe("createSelectedContentCaptureService", () => {
  it("捕获选中文本时会写入 txt 材料并恢复原剪贴板", async () => {
    const clipboard = createClipboardPort({
      afterCopyText: "hello selected text",
      initialText: "old clipboard"
    });
    const writeTextFile = vi.fn(async () => "G:\\materials\\selected-text-1.txt");
    const managedAssetStore = createManagedAssetStore("asset-text-1");
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore,
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: {
        writeTextFile,
        writeJsonFile: vi.fn(),
        writePngFile: vi.fn()
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      },
      idGenerator: {
        nextId: () => "capture-text-1"
      }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([
      {
        kind: "selected_text",
        source: "selection_capture",
        display_name: "选中文本.txt",
        path: "G:\\materials\\selected-text-1.txt",
        managed_asset_id: "asset-text-1"
      }
    ]);
    expect(writeTextFile).toHaveBeenCalledWith(
      "G:\\materials",
      "selected-text-20260709-020000-capture-text-1.txt",
      "hello selected text"
    );
    expect(clipboard.currentText()).toBe("old clipboard");
    expect(clipboard.events()).toEqual(["snapshot", "clear", "copy", "restore"]);
  });

  it("捕获选中图片时会写入 png 材料并恢复原剪贴板", async () => {
    const clipboard = createClipboardPort({
      afterCopyImagePng: Buffer.from("new-image"),
      initialImagePng: Buffer.from("old-image")
    });
    const writePngFile = vi.fn(async () => "G:\\materials\\selected-image-1.png");
    const managedAssetStore = createManagedAssetStore("asset-image-1");
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore,
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: {
        writeTextFile: vi.fn(),
        writeJsonFile: vi.fn(),
        writePngFile
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      },
      idGenerator: {
        nextId: () => "capture-image-1"
      }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([
      {
        kind: "selected_image",
        source: "selection_capture",
        display_name: "选中图片.png",
        path: "G:\\materials\\selected-image-1.png",
        managed_asset_id: "asset-image-1"
      }
    ]);
    expect(writePngFile).toHaveBeenCalledWith(
      "G:\\materials",
      "selected-image-20260709-020000-capture-image-1.png",
      Buffer.from("new-image")
    );
    expect(clipboard.currentImagePng()).toEqual(Buffer.from("old-image"));
  });

  it("捕获网页混合选区时只保存单个有序 JSON，不下载远程图片", async () => {
    const clipboard = createClipboardPort({
      afterCopyText: "标题 https://example.com/doc",
      afterCopyHtml: '<article><h1>标题</h1><img src="https://example.com/a.png"></article>',
      initialText: "old clipboard"
    });
    const writeTextFile = vi.fn(async (_directory: string, fileName: string) => `G:\\materials\\${fileName}`);
    const writeJsonFile = vi.fn(async (_directory: string, fileName: string) => `G:\\materials\\${fileName}`);
    const managedAssetStore = createManagedAssetStore("asset-web-1");
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore,
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: {
        writeTextFile,
        writeJsonFile,
        writePngFile: vi.fn()
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      },
      idGenerator: {
        nextId: () => "web-selection-1"
      }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([
      {
        kind: "selected_web",
        source: "selection_capture",
        display_name: "网页选区",
        path: "G:\\materials\\selected-web-20260709-020000-web-selection-1.json",
        managed_asset_id: "asset-web-1"
      }
    ]);
    expect(writeTextFile).not.toHaveBeenCalled();
    expect(writeJsonFile).toHaveBeenCalledWith(
      "G:\\materials",
      "selected-web-20260709-020000-web-selection-1.json",
      {
        version: 2,
        plain_text: "标题 https://example.com/doc",
        parts: [
          { type: "text", text: "标题" },
          { type: "image_url", url: "https://example.com/a.png" }
        ]
      }
    );
    expect(clipboard.currentText()).toBe("old clipboard");
  });

  it("Edge 地址栏选区会把带参数和片段的原始 URL 逐字写入文本材料", async () => {
    const originalUrl = "https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333.1007#reply42";
    const clipboard = createClipboardPort({
      afterCopyText: originalUrl,
      afterCopyHtml: `<a href="${originalUrl}">大疆 vlog｜我在交大的读研生活_哔哩哔哩_bilibili</a>`,
      initialText: "old clipboard"
    });
    const info = vi.fn();
    const fileStore = createFileStore();
    fileStore.writeTextFile.mockResolvedValue("G:\\materials\\selected-text.txt");
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore("asset-url-1"),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore,
      clock: { now: () => new Date("2026-07-09T10:00:00+08:00") },
      idGenerator: { nextId: () => "url-text-1" },
      logger: { info }
    });

    await expect(service.captureSelectedContent()).resolves.toHaveLength(1);
    expect(fileStore.writeTextFile).toHaveBeenCalledWith(
      "G:\\materials",
      "selected-text-20260709-020000-url-text-1.txt",
      originalUrl
    );
    expect(info).toHaveBeenCalledWith("[companion-main] selection capture completed", {
      resultKind: "text",
      itemCount: 1,
      clipboardChanged: true,
      elapsedMs: 42,
      timedOut: false,
      standaloneHttpUrl: true,
      linkCount: 1,
      imageCount: 0,
      fallbackReason: "standalone_url_matches_html_link"
    });
    expect(JSON.stringify(info.mock.calls)).not.toContain(originalUrl);
    expect(JSON.stringify(info.mock.calls)).not.toContain("大疆 vlog");
    expect(clipboard.currentText()).toBe("old clipboard");
  });

  it("捕获不含图片的网页富文本时直接保存为文本材料", async () => {
    const clipboard = createClipboardPort({
      afterCopyText: "标题\n正文",
      afterCopyHtml: "<article><h1>标题</h1><p>正文</p></article>"
    });
    const fileStore = createFileStore();
    fileStore.writeTextFile.mockResolvedValue("G:\\materials\\selected-text.txt");
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore("asset-text-1"),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore,
      clock: { now: () => new Date("2026-07-09T10:00:00+08:00") },
      idGenerator: { nextId: () => "rich-text-1" }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([
      {
        kind: "selected_text",
        source: "selection_capture",
        display_name: "选中文本.txt",
        path: "G:\\materials\\selected-text.txt",
        managed_asset_id: "asset-text-1"
      }
    ]);
    expect(fileStore.writeJsonFile).not.toHaveBeenCalled();
    expect(fileStore.writeTextFile).toHaveBeenCalledWith(
      "G:\\materials",
      "selected-text-20260709-020000-rich-text-1.txt",
      "标题\n正文"
    );
  });

  it("捕获桌面选中文件时会优先返回原路径文件材料", async () => {
    const info = vi.fn();
    const clipboard = createClipboardPort({
      afterCopyText: "notes.txt",
      afterCopyHtml: "<p>notes.txt</p>",
      afterCopyFilePaths: ["G:\\docs\\notes.txt"],
      initialText: "old clipboard"
    });
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore(),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: createFileStore(),
      pathInspector: {
        isDirectory: path => path.endsWith("project")
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      },
      logger: { info }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([
      {
        kind: "dragged_file",
        source: "selection_capture",
        display_name: "notes.txt",
        path: "G:\\docs\\notes.txt",
      }
    ]);
    expect(clipboard.events()).toEqual(["snapshot", "clear", "copy", "restore"]);
    expect(clipboard.currentText()).toBe("old clipboard");
    expect(info).toHaveBeenCalledWith("[companion-main] selection capture completed", {
      resultKind: "paths",
      itemCount: 1,
      clipboardChanged: true,
      elapsedMs: 42,
      timedOut: false,
      standaloneHttpUrl: false,
      linkCount: 0,
      imageCount: 0,
      fallbackReason: null
    });
    expect(JSON.stringify(info.mock.calls)).not.toContain("G:\\\\docs\\\\notes.txt");
  });

  it("捕获多个文件和文件夹时会分别返回 file 与 folder 材料", async () => {
    const clipboard = createClipboardPort({
      afterCopyFilePaths: ["G:\\docs\\notes.txt", "G:\\workspace\\project"]
    });
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore(),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: createFileStore(),
      pathInspector: {
        isDirectory: path => path.endsWith("project")
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([
      {
        kind: "dragged_file",
        source: "selection_capture",
        display_name: "notes.txt",
        path: "G:\\docs\\notes.txt",
      },
      {
        kind: "dragged_folder",
        source: "selection_capture",
        display_name: "project",
        path: "G:\\workspace\\project",
      }
    ]);
  });

  it("捕获前剪贴板是文件列表时会在结束后恢复文件列表", async () => {
    const clipboard = createClipboardPort({
      initialFilePaths: ["G:\\old\\before.txt"],
      afterCopyText: "hello selected text"
    });
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore(),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: createFileStore(),
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      },
      idGenerator: {
        nextId: () => "capture-text-1"
      }
    });

    await expect(service.captureSelectedContent()).resolves.toHaveLength(1);
    expect(clipboard.currentFilePaths()).toEqual(["G:\\old\\before.txt"]);
  });

  it("没有选中内容时不会把旧剪贴板作为材料", async () => {
    const clipboard = createClipboardPort({
      initialText: "old clipboard"
    });
    const info = vi.fn();
    const service = createSelectedContentCaptureService({
      platform: "win32",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore(),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: {
        writeTextFile: vi.fn(),
        writeJsonFile: vi.fn(),
        writePngFile: vi.fn()
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      },
      logger: { info }
    });

    await expect(service.captureSelectedContent()).resolves.toEqual([]);
    expect(clipboard.currentText()).toBe("old clipboard");
    expect(info).toHaveBeenCalledWith("[companion-main] selection capture completed", {
      resultKind: "empty",
      itemCount: 0,
      clipboardChanged: false,
      elapsedMs: 1500,
      timedOut: true,
      standaloneHttpUrl: false,
      linkCount: 0,
      imageCount: 0,
      fallbackReason: null
    });
    expect(JSON.stringify(info.mock.calls)).not.toContain("old clipboard");
  });

  it("非 Windows 平台返回 unsupported 且不触碰剪贴板", async () => {
    const clipboard = createClipboardPort({
      initialText: "old clipboard"
    });
    const service = createSelectedContentCaptureService({
      platform: "darwin",
      attachmentDir: "G:\\materials",
      managedAssetStore: createManagedAssetStore(),
      clipboard,
      selectionCapture: clipboard.selectionCapture,
      fileStore: {
        writeTextFile: vi.fn(),
        writeJsonFile: vi.fn(),
        writePngFile: vi.fn()
      },
      clock: {
        now: () => new Date("2026-07-09T10:00:00+08:00")
      }
    });

    await expect(service.captureSelectedContent()).rejects.toThrow("selection capture is unsupported on darwin");
    expect(clipboard.events()).toEqual([]);
  });

  it("Electron 剪贴板适配器会通过 FileDrop 端口恢复文件列表", async () => {
    const fileDropPort = {
      readFilePaths: vi.fn(async () => ["G:\\old\\before.txt"]),
      writeFilePaths: vi.fn(async () => undefined)
    };
    const electronClipboard = {
      availableFormats: vi.fn(() => ["FileNameW"]),
      readText: vi.fn(() => ""),
      writeText: vi.fn(),
      readHTML: vi.fn(() => ""),
      writeHTML: vi.fn(),
      readImage: vi.fn(() => ({
        isEmpty: () => true,
        toPNG: () => Buffer.from("")
      })),
      writeImage: vi.fn(),
      clear: vi.fn()
    };
    const port = createElectronSelectedContentClipboardPort(
      electronClipboard as unknown as Parameters<typeof createElectronSelectedContentClipboardPort>[0],
      fileDropPort
    );
    const snapshot = await port.snapshot();

    await port.restore(snapshot);

    expect(fileDropPort.writeFilePaths).toHaveBeenCalledWith(["G:\\old\\before.txt"]);
    expect(electronClipboard.writeText).not.toHaveBeenCalled();
    expect(electronClipboard.writeImage).not.toHaveBeenCalled();
  });

  it("普通剪贴板快照不会启动 FileDrop 辅助进程", async () => {
    const fileDropPort = {
      readFilePaths: vi.fn(async () => ["G:\\old\\before.txt"]),
      writeFilePaths: vi.fn(async () => undefined)
    };
    const electronClipboard = {
      availableFormats: vi.fn(() => ["text/plain"]),
      readText: vi.fn(() => "普通文本"),
      writeText: vi.fn(),
      readHTML: vi.fn(() => ""),
      writeHTML: vi.fn(),
      readImage: vi.fn(() => ({ isEmpty: () => true, toPNG: () => Buffer.from("") })),
      writeImage: vi.fn(),
      clear: vi.fn()
    };
    const port = createElectronSelectedContentClipboardPort(
      electronClipboard as unknown as Parameters<typeof createElectronSelectedContentClipboardPort>[0],
      fileDropPort
    );

    await expect(port.snapshot()).resolves.toMatchObject({ text: "普通文本", filePaths: [] });
    expect(fileDropPort.readFilePaths).not.toHaveBeenCalled();
  });
});

function createClipboardPort(input: {
  initialText?: string;
  initialImagePng?: Buffer;
  initialFilePaths?: string[];
  afterCopyText?: string;
  afterCopyHtml?: string;
  afterCopyImagePng?: Buffer;
  afterCopyFilePaths?: string[];
}) {
  let text = input.initialText ?? "";
  let html = "";
  let imagePng = input.initialImagePng ?? null;
  let filePaths = input.initialFilePaths ?? [];
  const eventsLog: string[] = [];

  const port = {
    snapshot() {
      eventsLog.push("snapshot");
      return {
        text,
        html,
        imagePng,
        filePaths
      };
    },
    clear() {
      eventsLog.push("clear");
      text = "";
      html = "";
      imagePng = null;
      filePaths = [];
    },
    async applyCopiedSelection() {
      eventsLog.push("copy");
      text = input.afterCopyText ?? "";
      html = input.afterCopyHtml ?? "";
      imagePng = input.afterCopyImagePng ?? null;
      filePaths = input.afterCopyFilePaths ?? [];
    },
    selectionCapture: {
      async captureSelection() {
        await port.applyCopiedSelection();
        return {
          filePaths: [...filePaths],
          clipboardChanged: Boolean(text || html || imagePng || filePaths.length > 0),
          elapsedMs: text || html || imagePng || filePaths.length > 0 ? 42 : 1500
        };
      }
    },
    readText() {
      return text;
    },
    readHtml() {
      return html;
    },
    readImagePng() {
      return imagePng;
    },
    restore(snapshot: { text: string; html: string; imagePng: Buffer | null; filePaths: string[] }) {
      eventsLog.push("restore");
      text = snapshot.text;
      html = "";
      imagePng = snapshot.imagePng;
      filePaths = snapshot.filePaths;
    },
    currentText() {
      return text;
    },
    currentImagePng() {
      return imagePng;
    },
    currentFilePaths() {
      return filePaths;
    },
    events() {
      return eventsLog;
    }
  };
  return port;
}

function createFileStore() {
  return {
    writeTextFile: vi.fn(),
    writeJsonFile: vi.fn(),
    writePngFile: vi.fn()
  };
}

function createManagedAssetStore(managedAssetId = "asset-test") {
  return {
    registerGeneratedAsset: vi.fn(() => managedAssetId),
    release: vi.fn(async () => true)
  };
}
