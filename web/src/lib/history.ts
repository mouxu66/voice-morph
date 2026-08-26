/** 轻量历史记录持久化（localStorage），刷新页面后保留最近记录 */

export function loadHistory<T>(key: string, limit: number): T[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(0, limit) : [];
  } catch {
    return [];
  }
}

export function saveHistory<T>(key: string, items: T[], limit: number): void {
  try {
    localStorage.setItem(key, JSON.stringify(items.slice(0, limit)));
  } catch {
    /* 存储不可用时静默失败 */
  }
}

/** 在数组头部插入一条记录并持久化，返回裁剪后的数组 */
export function prependHistory<T>(key: string, items: T[], item: T, limit: number): T[] {
  const next = [item, ...items].slice(0, limit);
  saveHistory(key, next, limit);
  return next;
}

export const STORAGE_KEYS = {
  ttsHistory: "vm-tts-history",
} as const;
