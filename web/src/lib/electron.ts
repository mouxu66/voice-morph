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

// ---- 模型配置 / 首启引导 ----
/** 配置项的种类：与主进程 setup-ipc.cjs 的 PICK_SPECS 键一致 */
export type SetupKind = "tts_models" | "tts_venv" | "rvc_root";

/** 检测结果里的一项 */
export type SetupItem = {
  key: SetupKind;
  label: string;
  ok: boolean;
  /** 当前生效路径（可能为空 = 完全没配） */
  path: string;
  /** 路径来源：config（用户配置）/ env（环境变量）/ derived（自动推导）/ none */
  source: "config" | "env" | "derived" | "none";
  reason: string;
};

export type SetupStatus = {
  config: {
    version: number;
    ttsModelsDir: string;
    ttsVenvPy: string;
    rvcRoot: string;
    setupSeen: boolean;
    /** 桌宠换装首启引导是否已看过（置 true 后不再自动开口） */
    petGuideSeen: boolean;
  };
  configPath: string;
  items: SetupItem[];
  ttsOk: boolean;
  rvcOk: boolean;
  allOk: boolean;
  /** 缺失项 key 列表，如 ["tts_models", "tts_venv"] */
  missing: SetupKind[];
  setupSeen: boolean;
};

/** 目录选择结果；ok=false 表示用户强行选了校验不过的路径 */
export type SetupPickResult = { canceled: boolean; path?: string; ok?: boolean; reason?: string };

// ---- 自动扫描与下载指引 ----

/** 指引里的一段操作步骤（command 可直接复制粘贴） */
export type GuideStep = { title: string; detail?: string; command?: string };
/** 指引里的一个官方链接（由主进程按 kind+序号 解析，渲染层拿不到任意 URL 的能力） */
export type GuideLink = { label: string; url: string; note?: string };

/** 某一项外部资源的「去哪下 / 下完长什么样」说明 */
export type ResourceGuide = {
  key: SetupKind;
  label: string;
  /** 体积（下载前就该知道，不该点下去才发现是 20 GB） */
  sizeText: string;
  why: string;
  /** 目标目录结构（等宽字体展示） */
  layout: string[];
  steps: GuideStep[];
  links: GuideLink[];
  /** 易踩的坑（如「--local-dir 不能省」） */
  notes: string[];
};

export type GuidesPayload = { verifiedAt: string; guides: ResourceGuide[] };

/** 扫描到的一个候选位置 */
export type ScanCandidate = {
  kind: SetupKind;
  /** 对 kind 为 tts_venv 时是 python.exe 路径，其余是目录 */
  path: string;
  score: number;
  reasons: string[];
  /** 同 kind 里得分最高者 */
  recommended?: boolean;
};

export type ScanStats = {
  dirsVisited: number;
  /** 是否触到目录数/时间预算上限（结果可能不全） */
  truncated: boolean;
  elapsedMs: number;
  maxDirs: number;
  roots: number;
};

export type ScanResult = {
  ok: boolean;
  reason?: string;
  candidates: Partial<Record<SetupKind, ScanCandidate[]>>;
  stats: ScanStats | null;
};

interface ElectronBridge {
  startBackend?: () => Promise<StartResult>;
  stopBackend?: () => Promise<{ ok: boolean }>;
  restartBackend?: () => Promise<{ running: boolean; reason?: string; reusedExternal?: boolean }>;
  backendStatus?: () => Promise<{ running: boolean; port: number }>;
  showBackendLog?: () => Promise<{ ok: boolean }>;
  petGuide?: (payload: PetGuidePayload) => void;
  // ---- 模型配置 / 首启引导 ----
  setupStatus?: () => Promise<SetupStatus>;
  setupGuides?: () => Promise<GuidesPayload>;
  setupOpenGuideLink?: (kind: SetupKind, index: number) => Promise<{ ok: boolean; url?: string; reason?: string }>;
  setupScan?: (opts?: { kinds?: SetupKind[] }) => Promise<ScanResult>;
  setupPickDir?: (kind: SetupKind) => Promise<SetupPickResult>;
  setupSave?: (patch: Partial<SetupStatus["config"]>) => Promise<{ ok: boolean } & SetupStatus>;
  setupDismiss?: () => Promise<{ ok: boolean } & SetupStatus>;
  setupRunWizard?: () => Promise<{ changed: boolean } & SetupStatus>;
  setupReset?: () => Promise<{ ok: boolean } & SetupStatus>;
  setupShowConfig?: () => Promise<{ ok: boolean; path: string }>;
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

/** 重启后端（改完模型配置后用）。非桌面端返回 null —— 调用方降级为提示手动重启。 */
export async function restartBackend(): Promise<{ running: boolean; reason?: string } | null> {
  if (!electron?.restartBackend) return null;
  try {
    return await electron.restartBackend();
  } catch {
    return null;
  }
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

// ---------------- 模型配置 / 首启引导 ----------------
// 非桌面端（网页 / Vite / 局域网）没有这套桥：getSetupStatus 返回 null，
// UI 自动降级为「显示配置说明 + 复制环境变量命令」。

/** 是否能在这个环境里读写模型配置（即跑在桌面壳里） */
export const hasSetup = Boolean(electron?.setupStatus);

/** 只读状态；非桌面端返回 null */
export async function getSetupStatus(): Promise<SetupStatus | null> {
  if (!electron?.setupStatus) return null;
  try {
    return await electron.setupStatus();
  } catch {
    return null;
  }
}

/** 弹目录选择器；非桌面端返回 null（调用方降级为手填路径） */
export async function pickSetupDir(kind: SetupKind): Promise<SetupPickResult | null> {
  if (!electron?.setupPickDir) return null;
  try {
    return await electron.setupPickDir(kind);
  } catch {
    return null;
  }
}

/** 保存配置，返回最新状态（失败返回 null） */
export async function saveSetup(patch: Partial<SetupStatus["config"]>): Promise<SetupStatus | null> {
  if (!electron?.setupSave) return null;
  try {
    return await electron.setupSave(patch);
  } catch {
    return null;
  }
}

/** 稍后配置：不再自动弹引导 */
export async function dismissSetup(): Promise<void> {
  if (!electron?.setupDismiss) return;
  try {
    await electron.setupDismiss();
  } catch {
    /* 忽略 */
  }
}

/** 在资源管理器中定位 config.json */
export async function showSetupConfig(): Promise<void> {
  if (!electron?.setupShowConfig) return;
  try {
    await electron.setupShowConfig();
  } catch {
    /* 忽略 */
  }
}

// ---------------- 自动扫描 / 下载指引 ----------------

/** 是否能自动扫描本机（桌面壳内才有；网页模式返回 false，UI 隐藏扫描入口） */
export const hasSetupScan = Boolean(electron?.setupScan);

/** 下载指引；非桌面端返回 null */
export async function getSetupGuides(): Promise<GuidesPayload | null> {
  if (!electron?.setupGuides) return null;
  try {
    return await electron.setupGuides();
  } catch {
    return null;
  }
}

/**
 * 扫描本机找可用资源。
 * `kinds` 留空 = 三项都找。扫描全程在主进程里异步进行，不冻结界面。
 */
export async function scanSetup(kinds?: SetupKind[]): Promise<ScanResult | null> {
  if (!electron?.setupScan) return null;
  try {
    return await electron.setupScan(kinds && kinds.length ? { kinds } : undefined);
  } catch {
    return null;
  }
}

/**
 * 打开指引里的某条链接。
 * 只传 kind + 序号：URL 由主进程从常量里查，渲染层没有打开任意地址的能力。
 */
export async function openGuideLink(kind: SetupKind, index: number): Promise<boolean> {
  if (!electron?.setupOpenGuideLink) return false;
  try {
    const r = await electron.setupOpenGuideLink(kind, index);
    return Boolean(r?.ok);
  } catch {
    return false;
  }
}
