/** 后端地址前缀与统一请求入口（2026-10-22 从 client.ts 顶部抽出）。
 *  `jsonFetch` 不对外导出到 client.ts barrel —— 它只是各域模块的内部通道。 */

// 后端统一挂在 /api 前缀下。全仓**只在这里**判定一次"该用相对还是绝对地址"
// （2026-09-13 收敛：此前 client.ts / useEffects.ts / useWorkshop.ts 各写了一份）。
//
// Electron(file://) 页面直连的后端地址。页面由 http(s) 提供时（dev 的 Vite 5173、
// 后端自托管的局域网页面）一律走同源相对路径，用不到它。
export const BACKEND_ORIGIN = "http://127.0.0.1:8000";

/**
 * 后端地址前缀：
 *   - dev(Vite 5173) / 页面由 http(s) 提供 → ""（同源相对路径；dev 由 vite proxy 转发）
 *   - Electron 打包态 file:// 页面 → BACKEND_ORIGIN（跨源直连本机 8000）
 *
 * 注意后者不能写成 `import.meta.env.DEV ? ... : BACKEND_ORIGIN`：局域网用户
 * （手机浏览器打开 http://<PC-IP>:8000）跑的是**构建产物**，DEV 为 false，
 * 那样会拼出 127.0.0.1 —— 在手机上指向手机自己，必然失败。判定依据必须是
 * "页面协议"，不是构建模式。
 */
export function backendPrefix(): string {
  if (import.meta.env.DEV) return "";
  if (typeof location !== "undefined" && (location.protocol === "http:" || location.protocol === "https:")) {
    return "";
  }
  return BACKEND_ORIGIN;
}

export const BASE = backendPrefix() + "/api";

// 把后端返回的相对音频路径（如 /media/outputs/x.wav）转成可播放的绝对地址。
// 生产环境后端返回的 url 是 /api/media/...，这里做兜底拼接。
export function mediaUrl(path: string): string {
  if (!path) return "";
  if (/^https?:\/\//.test(path)) return path;
  if (path.startsWith("/api/")) return backendPrefix() + path;
  // 旧格式相对路径（/media/...），拼上后端前缀
  return BASE + path;
}

export async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + url, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}
