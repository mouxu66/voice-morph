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

interface ElectronBridge {
  startBackend?: () => Promise<StartResult>;
  stopBackend?: () => Promise<{ ok: boolean }>;
  backendStatus?: () => Promise<{ running: boolean; port: number }>;
  showBackendLog?: () => Promise<{ ok: boolean }>;
  petGuide?: (payload: PetGuidePayload) => void;
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
