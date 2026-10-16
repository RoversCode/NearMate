const WINDOWS_CPU_WARNING_TEXT =
  "CPU monitoring unsupported on current platform. Server capacity management will be disabled";
const ISO_TIMESTAMP_LINE_PATTERN =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\s+/;
const ANSI_ESCAPE_PATTERN = /\u001B\[[0-?]*[ -/]*[@-~]/g;
const GO_STACK_FUNCTION_LINE_PATTERN = /^(?:github\.com\/\S+|go\.uber\.org\/\S+|main\.[A-Za-z_]\w*|runtime\.[A-Za-z_]\w*)$/;
const GO_STACK_SOURCE_LINE_PATTERN = /^\s+\S+\.go:\d+(?::\d+)?\s*$/;

/**
 * 创建 LiveKit 开发日志过滤器。
 * 这个过滤器只处理 Windows 版 LiveKit 已知的 CPU 监控提示，避免把非致命能力缺失显示成带堆栈的错误。
 *
 * @param {object} options 过滤器配置。
 * @param {string} options.platform 当前运行平台，默认使用 Node.js 的平台值。
 * @param {(text: string) => void} options.write 输出未被过滤的日志文本。
 * @param {() => void} options.onKnownWindowsCpuWarning 发现已知提示时调用一次的回调。
 * @returns {{push: (chunk: string | Buffer) => void, flush: () => void}} 可接收分块文本的过滤器。
 */
export function createLiveKitLogFilter({
  platform = process.platform,
  write,
  onKnownWindowsCpuWarning,
}) {
  let pendingText = "";
  let suppressingKnownWarning = false;
  let reportedKnownWarning = false;

  /**
   * 判断一行是否是下一条带时间戳的 LiveKit 日志。
   * 过滤器必须在遇到下一条正式日志时恢复转发，否则会把后续真实错误一并吞掉。
   *
   * @param {string} line 待判断的日志行。
   * @returns {boolean} 是否是带 ISO 时间戳的日志行。
   */
  function isTimestampedLiveKitLog(line) {
    return ISO_TIMESTAMP_LINE_PATTERN.test(line.replace(ANSI_ESCAPE_PATTERN, ""));
  }

  /**
   * 判断一行是否是 Go 堆栈内容。
   * 真实错误可能没有时间戳，所以不能只依赖“下一条时间戳”来结束过滤。
   *
   * @param {string} line 已去除换行符的日志行。
   * @returns {boolean} 是否属于当前已知提示后面的 Go 堆栈。
   */
  function isGoStackTraceLine(line) {
    const normalizedLine = line.replace(ANSI_ESCAPE_PATTERN, "").trimEnd();
    return (
      normalizedLine.trim() === ""
      || GO_STACK_FUNCTION_LINE_PATTERN.test(normalizedLine.trim())
      || GO_STACK_SOURCE_LINE_PATTERN.test(normalizedLine)
    );
  }

  /**
   * 处理一整行日志。
   * 已知 CPU 提示后面紧跟的是 Go 堆栈，因此只跳过直到下一条带时间戳的日志。
   *
   * @param {string} line 已去除换行符的日志行。
   * @param {string} lineEnding 当前日志行原本使用的换行符，可以为空。
   * @returns {void}
   */
  function processLine(line, lineEnding) {
    if (platform === "win32" && line.includes(WINDOWS_CPU_WARNING_TEXT)) {
      suppressingKnownWarning = true;
      if (!reportedKnownWarning) {
        reportedKnownWarning = true;
        onKnownWindowsCpuWarning();
      }
      return;
    }

    if (suppressingKnownWarning) {
      if (!isTimestampedLiveKitLog(line) && isGoStackTraceLine(line)) {
        return;
      }
      suppressingKnownWarning = false;
    }

    write(`${line}${lineEnding}`);
  }

  return {
    /**
     * 接收子进程输出的一块文本，并在内部按完整行处理。
     *
     * @param {string | Buffer} chunk 子进程输出分块。
     * @returns {void}
     */
    push(chunk) {
      pendingText += chunk.toString();
      let lineMatch = /^(.*?)(\r\n|\n)/s.exec(pendingText);
      while (lineMatch) {
        processLine(lineMatch[1], lineMatch[2]);
        pendingText = pendingText.slice(lineMatch[0].length);
        lineMatch = /^(.*?)(\r\n|\n)/s.exec(pendingText);
      }
    },

    /**
     * 在输出流关闭时处理最后一行不带换行符的文本。
     *
     * @returns {void}
     */
    flush() {
      if (pendingText) {
        processLine(pendingText, "");
        pendingText = "";
      }
    },
  };
}

export { WINDOWS_CPU_WARNING_TEXT };
