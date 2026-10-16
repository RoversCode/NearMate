import { clipboard, nativeImage } from "electron";

type Awaitable<T> = T | Promise<T>;

type ClipboardSnapshot = {
  text: string;
  html: string;
  imagePng: Buffer | null;
  filePaths: string[];
};

export type CapturedClipboardContent = Omit<ClipboardSnapshot, "filePaths"> & SelectionCopyResult;

export type SelectionCopyResult = {
  filePaths: string[];
  clipboardChanged: boolean;
  elapsedMs: number;
};

export type SelectedContentFileDropPort = {
  readFilePaths(): Promise<string[]>;
  writeFilePaths(filePaths: string[]): Promise<void>;
};

export type SelectedContentClipboardPort = {
  snapshot(): Awaitable<ClipboardSnapshot>;
  clear(): Awaitable<void>;
  readText(): string;
  readHtml(): string;
  readImagePng(): Buffer | null;
  restore(snapshot: ClipboardSnapshot): Awaitable<void>;
};

export type WindowsSelectionCapturePort = {
  captureSelection(): Promise<SelectionCopyResult>;
};

/**
 * 在严格的 try/finally 边界内复制当前系统选区并恢复原剪贴板。
 *
 * @param platform 当前 Node 平台。
 * @param clipboardPort 剪贴板读写端口。
 * @param selectionCapture Windows 复制与 FileDrop 捕获端口。
 * @returns 复制动作生成的原始内容，不创建项目资产。
 */
export async function captureSelectionClipboardTransaction(
  platform: NodeJS.Platform,
  clipboardPort: SelectedContentClipboardPort,
  selectionCapture: WindowsSelectionCapturePort
): Promise<CapturedClipboardContent> {
  if (platform !== "win32") {
    throw new Error(`selection capture is unsupported on ${platform}`);
  }

  const original = await clipboardPort.snapshot();
  await clipboardPort.clear();
  try {
    const copyResult = await selectionCapture.captureSelection();
    return {
      ...copyResult,
      text: clipboardPort.readText(),
      html: clipboardPort.readHtml(),
      imagePng: clipboardPort.readImagePng()
    };
  } finally {
    await clipboardPort.restore(original);
  }
}

/**
 * 创建 Electron 剪贴板端口，仅在格式表确认存在文件列表时访问 FileDrop。
 *
 * @param electronClipboard Electron 剪贴板窄接口。
 * @param fileDropPort Windows 文件列表端口。
 * @returns 可用于选区事务的剪贴板端口。
 */
export function createElectronSelectedContentClipboardPort(
  electronClipboard: Pick<typeof clipboard, "availableFormats" | "readText" | "writeText" | "readHTML" | "writeHTML" | "readImage" | "writeImage" | "clear">,
  fileDropPort?: SelectedContentFileDropPort
): SelectedContentClipboardPort {
  return {
    async snapshot() {
      const image = electronClipboard.readImage();
      return {
        text: electronClipboard.readText(),
        html: electronClipboard.readHTML(),
        imagePng: image.isEmpty() ? null : image.toPNG(),
        filePaths: fileDropPort && hasWindowsFileDrop(electronClipboard.availableFormats())
          ? await fileDropPort.readFilePaths()
          : []
      };
    },
    clear: () => electronClipboard.clear(),
    readText: () => electronClipboard.readText(),
    readHtml: () => electronClipboard.readHTML(),
    readImagePng() {
      const image = electronClipboard.readImage();
      return image.isEmpty() ? null : image.toPNG();
    },
    async restore(snapshot) {
      electronClipboard.clear();
      if (snapshot.filePaths.length > 0 && fileDropPort) {
        await fileDropPort.writeFilePaths(snapshot.filePaths);
        return;
      }
      if (snapshot.imagePng?.byteLength) {
        electronClipboard.writeImage(nativeImage.createFromBuffer(snapshot.imagePng));
        return;
      }
      if (snapshot.text) electronClipboard.writeText(snapshot.text);
      if (snapshot.html) electronClipboard.writeHTML(snapshot.html);
    }
  };
}

function hasWindowsFileDrop(formats: string[]): boolean {
  return formats.some(format => format === "FileNameW" || format === "FileName" || format === "CF_HDROP");
}
