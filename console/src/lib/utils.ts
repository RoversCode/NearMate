import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * 合并 Tailwind 类名：clsx 负责条件拼接，twMerge 负责去重冲突类。
 *
 * Args:
 *   inputs: 任意数量的类名、条件对象或数组。
 *
 * Returns:
 *   合并后的类名字符串，后传入的同组工具类覆盖先传入的。
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
