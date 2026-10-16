/** 保留 HTTP 状态码和后端 detail，方便页面对冲突做可恢复处理。 */
export class HttpApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string, message: string) {
    super(message);
    this.name = "HttpApiError";
    this.status = status;
    this.detail = detail;
  }
}

/**
 * 读取控制台 JSON 接口并统一处理 HTTP 错误。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   path: 接口路径。
 *   errorLabel: 面向用户的中文错误前缀。
 *
 * Returns:
 *   调用方指定的 JSON 类型。
 */
export async function fetchJson<T>(baseUrl: string, path: string, errorLabel: string): Promise<T> {
  return fetchJsonWithInit<T>(baseUrl, path, undefined, errorLabel);
}

/**
 * 读取控制台 JSON 接口并允许传入 fetch init。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   path: 接口路径。
 *   init: 可选请求配置。
 *   errorLabel: 面向用户的中文错误前缀。
 *
 * Returns:
 *   调用方指定的 JSON 类型。
 */
export async function fetchJsonWithInit<T>(
  baseUrl: string,
  path: string,
  init: RequestInit | undefined,
  errorLabel: string
): Promise<T> {
  const normalizedBaseUrl = baseUrl.replace(/\/$/, "");

  // 所有控制台接口均为同源请求；统一拼接能避免每个页面重复处理尾随斜杠。
  const requestUrl = `${normalizedBaseUrl}${path}`;
  const response = init === undefined ? await fetch(requestUrl) : await fetch(requestUrl, init);

  // 后端不可用或路由缺失时立即失败，避免控制台误把异常当作“暂无记录”。
  if (!response.ok) {
    const detail = await readHttpErrorDetail(response);
    throw new HttpApiError(response.status, detail, `${errorLabel}：${detail}`);
  }

  return (await response.json()) as T;
}

/**
 * 发送 JSON POST 请求并解析 JSON 响应。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   path: 接口路径。
 *   payload: JSON 请求体。
 *   errorLabel: 面向用户的中文错误前缀。
 *
 * Returns:
 *   响应 JSON。
 */
export async function postJson<T>(baseUrl: string, path: string, payload: object, errorLabel: string): Promise<T> {
  return fetchJsonWithInit<T>(
    baseUrl,
    path,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    },
    errorLabel
  );
}

/**
 * 发送 JSON PUT 请求并解析 JSON 响应。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   path: 接口路径。
 *   payload: JSON 请求体。
 *   errorLabel: 面向用户的中文错误前缀。
 *
 * Returns:
 *   响应 JSON。
 */
export async function putJson<T>(baseUrl: string, path: string, payload: object, errorLabel: string): Promise<T> {
  return fetchJsonWithInit<T>(
    baseUrl,
    path,
    {
      method: "PUT",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    },
    errorLabel
  );
}

/**
 * 发送 JSON PATCH 请求并解析 JSON 响应。
 *
 * Args:
 *   baseUrl: 控制面根地址。
 *   path: 接口路径。
 *   payload: JSON 请求体。
 *   errorLabel: 面向用户的中文错误前缀。
 *
 * Returns:
 *   响应 JSON。
 */
export async function patchJson<T>(baseUrl: string, path: string, payload: object, errorLabel: string): Promise<T> {
  return fetchJsonWithInit<T>(
    baseUrl,
    path,
    {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    },
    errorLabel
  );
}

/**
 * 读取 HTTP 错误响应中的后端 detail。
 *
 * Args:
 *   response: 非 2xx 的 fetch 响应。
 *
 * Returns:
 *   优先返回 FastAPI JSON detail，否则回退到 HTTP 状态码。
 */
export async function readHttpErrorDetail(response: Response): Promise<string> {
  const fallbackMessage = `HTTP ${response.status}`;
  const contentType = response.headers.get("Content-Type") ?? response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    return fallbackMessage;
  }

  try {
    const payload: unknown = await response.json();
    const detail = extractHttpErrorDetail(payload);
    if (detail !== null) {
      return detail;
    }
  } catch {
    return fallbackMessage;
  }

  return fallbackMessage;
}

/**
 * 把 FastAPI 常见的字符串、对象和校验错误数组压缩成一条可读提示。
 *
 * Args:
 *   payload: 已解析的错误响应 JSON。
 *
 * Returns:
 *   可直接展示的错误详情；无法识别时返回 null。
 */
function extractHttpErrorDetail(payload: unknown): string | null {
  if (payload === null || typeof payload !== "object") {
    return typeof payload === "string" && payload.trim() ? payload.trim() : null;
  }

  if (Array.isArray(payload)) {
    return formatValidationErrors(payload);
  }

  const record = payload as Record<string, unknown>;
  const detail = record.detail;
  if (typeof detail === "string" && detail.trim()) {
    return detail.trim();
  }
  if (detail !== null && typeof detail === "object") {
    if (Array.isArray(detail)) {
      return formatValidationErrors(detail);
    }
    const detailRecord = detail as Record<string, unknown>;
    const message = detailRecord.message;
    if (typeof message === "string" && message.trim()) {
      return message.trim();
    }
    const code = detailRecord.code;
    if (typeof code === "string" && code.trim()) {
      // 版本冲突响应通常没有 message，但 code 足以帮助用户判断下一步。
      if (code === "settings_revision_conflict") {
        const expectedRevision = formatErrorValue(detailRecord.expected_revision);
        const actualRevision = formatErrorValue(detailRecord.actual_revision);
        if (expectedRevision !== null && actualRevision !== null) {
          return `设置已被其他操作更新（当前版本 ${actualRevision}，提交版本 ${expectedRevision}），请刷新后重试`;
        }
        return "设置已被其他操作更新，请刷新后重试";
      }
      return code.trim();
    }
  }

  const message = record.message;
  return typeof message === "string" && message.trim() ? message.trim() : null;
}

/** 将 FastAPI 422 的 detail 数组转换为字段和原因组成的短句。 */
function formatValidationErrors(errors: unknown[]): string | null {
  const messages = errors.flatMap((error) => {
    if (error === null || typeof error !== "object" || Array.isArray(error)) {
      return [];
    }
    const record = error as Record<string, unknown>;
    const message = typeof record.msg === "string" ? record.msg.trim() : "";
    if (!message) {
      return [];
    }
    const location = Array.isArray(record.loc)
      ? record.loc.filter((part): part is string | number => typeof part === "string" || typeof part === "number").join(".")
      : "";
    return [location ? `${location}: ${message}` : message];
  });
  return messages.length > 0 ? messages.join("；") : null;
}

function formatErrorValue(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return null;
}
