import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

/**
 * shadcn/ui 官方实现：clsx 负责条件拼类，tailwind-merge 负责
 * 冲突类去重（如 px-2 px-4 保留后者）。缺一不可。
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
