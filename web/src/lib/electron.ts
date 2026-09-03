// Electron 原生桥：仅在打包桌面端（main.cjs 注入 window.electron）存在。
// 网页 / Vite / 局域网模式下为 undefined，UI 自动降级为「显示启动命令 + 复制」。

export type StartResult = {
  attempted: boolean;
  running?: boolean;
  reusedExternal?: boolean;
  python?: string | null;
  reason?: string;
};

/** 桌宠页面导览载荷：切页时推给桌宠窗口，由它现身介绍并做动作 */
export type PetGuidePayload = {
  page: string;
  title: string;
  lines: string[];
  /** 精灵图动作：idle / listen / think / play / build / error */
  action: string;
  /** 外层 CSS 动作：pop / wave / lean / nod / sway / jump / twirl / shake / float / wiggle / flip / bounce / dance / ear / work */
  motion: string;
  /** 整段导览预计时长（ms），主进程据此决定窗口何时退场 */
  duration: number;
};

/** 更新清单里的一个版本（由 latest.json 提供） */
export type UpdateManifest = {
  version: string;
  /** 更新内容（更新页主体，支持多行文本 / `- ` 列表 / `### ` 小标题） */
  notes: string;
  pub_date?: string;
  url?: string;
  sha256?: string;
  size?: number;
  /** 强制更新：为 true 时不给「跳过此版本」 */
  mandatory?: boolean;
};

/** 检查结果 */
export type UpdateCheck = {
  ok: boolean;
  /** false = 没配 VM_UPDATE_URL，当前是纯本地模式（不算错误） */
  configured: boolean;
  hasUpdate: boolean;
  current: string;
  latest: UpdateManifest | null;
  reason?: string;
};

export type UpdateDownload = { ok: boolean; file?: string; cached?: boolean; reason?: string };
export type UpdateProgress = { pct: number; received?: number; total?: number; done?: boolean };

interface ElectronBridge {
  startBackend?: () => Promise<StartResult>;
  stopBackend?: () => Promise<{ ok: boolean }>;
  backendStatus?: () => Promise<{ running: boolean; port: number }>;
  showBackendLog?: () => Promise<{ ok: boolean }>;
  petGuide?: (payload: PetGuidePayload) => void;
  // ---- 自动更新 ----
  appVersion?: () => Promise<string>;
  updateCheck?: () => Promise<UpdateCheck>;
  updateDownload?: (manifest: UpdateManifest) => Promise<UpdateDownload>;
  updateInstall?: (file: string) => Promise<{ ok: boolean; reason?: string }>;
  updateSkip?: (version: string) => Promise<{ ok: boolean }>;
  onUpdateProgress?: (cb: (p: UpdateProgress) => void) => () => void;
  onUpdateAvailable?: (cb: (r: UpdateCheck) => void) => () => void;
}

const w = typeof window !== "undefined" ? (window as unknown as { electron?: ElectronBridge }) : undefined;
export const electron: ElectronBridge | undefined = w?.electron;

/** 是否运行在能真正拉起后端的桌面壳里 */
export const hasElectron = Boolean(electron?.startBackend || electron?.backendStatus);

export async function startBackend(): Promise<StartResult | null> {
  if (!electron?.startBackend) return null;
  return electron.startBackend();
}

export async function stopBackend(): Promise<void> {
  if (electron?.stopBackend) await electron.stopBackend();
}

export async function showBackendLog(): Promise<void> {
  if (electron?.showBackendLog) await electron.showBackendLog();
}

/** 让桌宠现身介绍当前页面；非桌面壳（纯网页）下静默跳过 */
export function petGuide(payload: PetGuidePayload): void {
  if (!electron?.petGuide) return;
  try {
    electron.petGuide(payload);
  } catch {
    // 桌宠窗口未就绪时忽略，不影响主界面
  }
}

// ---------------- 应用自动更新 ----------------
// 仅打包桌面端可用；网页/Vite/局域网模式下这些函数都返回 null（UI 自动隐藏入口）。

/** 是否能在这个环境里检查更新（即跑在带更新能力的桌面壳里） */
export const hasUpdate = Boolean(electron?.updateCheck);

export async function appVersion(): Promise<string | null> {
  if (!electron?.appVersion) return null;
  try {
    return await electron.appVersion();
  } catch {
    return null;
  }
}

export async function checkUpdate(): Promise<UpdateCheck | null> {
  if (!electron?.updateCheck) return null;
  return electron.updateCheck();
}

export async function downloadUpdate(manifest: UpdateManifest): Promise<UpdateDownload | null> {
  if (!electron?.updateDownload) return null;
  return electron.updateDownload(manifest);
}

export async function installUpdate(file: string): Promise<{ ok: boolean; reason?: string } | null> {
  if (!electron?.updateInstall) return null;
  return electron.updateInstall(file);
}

export async function skipUpdate(version: string): Promise<void> {
  if (!electron?.updateSkip) return;
  await electron.updateSkip(version);
}

/** 订阅下载进度，返回取消订阅函数（直接丢给 useEffect 的 return） */
export function onUpdateProgress(cb: (p: UpdateProgress) => void): () => void {
  if (!electron?.onUpdateProgress) return () => {};
  return electron.onUpdateProgress(cb);
}

/** 订阅「启动静默检查发现新版本」（用于自动弹更新页） */
export function onUpdateAvailable(cb: (r: UpdateCheck) => void): () => void {
  if (!electron?.onUpdateAvailable) return () => {};
  return electron.onUpdateAvailable(cb);
}
