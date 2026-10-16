/**
 * 窗口种类常量同时服务于运行时与编译期。
 * 这样测试、配置和主进程契约都能围绕同一份真相源收敛，避免字符串列表各自漂移。
 */
export const WINDOW_KINDS = ["avatar", "panel", "chat"] as const;

/**
 * WindowKind 直接从窗口种类常量推导。
 * 这样一旦未来增删窗口类型，类型系统会强制相关映射同步更新。
 */
export type WindowKind = (typeof WINDOW_KINDS)[number];
