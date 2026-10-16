export type MaterialIssueCategory = "unsupported" | "unreadable" | "duplicate";

export type MaterialAdmissionIssue = {
  submittedIndex: number;
  displayName: string;
  category: MaterialIssueCategory;
  reason: string;
};

export type MaterialFailureReportItem = {
  materialId?: string;
  displayName: string;
  category: Exclude<MaterialIssueCategory, "duplicate">;
  reason: string;
  message: string;
};

export type MaterialFailureReport = {
  phase: "admission" | "preflight";
  items: MaterialFailureReportItem[];
};

export type MaterialFailureReportSnapshot = {
  revision: number;
  report: MaterialFailureReport | null;
};

/**
 * 把 Backend 稳定原因转换为 Desktop 唯一用户文案。
 *
 * @param reason Backend 返回的事实原因码。
 * @returns 不包含技术异常和本地路径的可行动说明。
 */
export function describeMaterialFailure(reason: string): string {
  const messages: Record<string, string> = {
    unsupported_extension: "暂不支持此类文件格式",
    missing_path: "文件已被移动或删除",
    missing_or_symlink: "文件已被移动、删除或是软链接",
    permission_denied: "没有读取权限，请检查文件权限",
    metadata_unreadable: "无法读取文件信息，请检查文件权限",
    symlink_not_allowed: "暂不支持软链接文件",
    not_regular_file: "所选路径不是普通文件",
    empty_file: "文件内容为空",
    invalid_web_selection: "网页选区内容无效",
    invalid_image: "图片已损坏或格式与扩展名不一致",
    image_too_large: "图片像素数量过大",
    invalid_or_encrypted_pdf: "文件已损坏或加密",
    invalid_or_encrypted_docx: "文件已损坏或加密",
    invalid_text_encoding: "文本编码不受支持，需要 UTF-8",
    empty_text: "文本没有有效内容",
    empty_folder: "文件夹中没有可处理文件",
    io_error: "文件无法读取，请确认文件未被其他程序占用",
    unreadable_file: "文件无法读取，请确认文件未被其他程序占用"
  };
  return messages[reason] ?? "文件无法读取，请确认文件未被其他程序占用";
}
