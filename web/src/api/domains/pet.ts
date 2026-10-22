import { jsonFetch } from "../http";

// ---- 桌面人偶市场（皮肤市场） ----

export type PetSkinItem = {
  id: string;
  name: string;
  category?: string;
  license?: string;
  attribution?: string;
  description?: string;
  source_type?: string;
  bundle?: boolean;
  size_hint_mb?: number;
};

export type PetInstalledItem = {
  id: string;
  name: string;
  category?: string;
  license?: string;
  attribution?: string;
  applied: boolean;
  preview?: string;
};

export type PetSkinState = { sheet: string; frames: number; dur: number };
export type PetAppliedSkin = {
  id: string;
  frameW: number;
  frameH: number;
  states: Record<string, PetSkinState>;
};

export type PetTaskItem = {
  skin_id: string;
  status: string;   // queued | downloading | installing | done | failed | cancelled | idle
  phase: string;
  message: string;
  percent: number;
  error: string;
};
export type PetProgress = {
  items: PetTaskItem[];
  active: number;   // 进行中（下载/转换）任务数
  queued: number;   // 排队任务数
};
export type PetSkinDetail = {
  item: {
    id: string;
    name: string;
    category?: string;
    license?: string;
    attribution?: string;
    description?: string;
    source_type?: string;
    bundle?: boolean;
  };
  source_urls: string[];
  installed: boolean;
  applied: boolean;
  frameW: number;
  frameH: number;
  states: Record<string, PetSkinState>;
  license_text: string;
};
export type PetSearchItem = PetSkinItem & { installed: boolean; applied: boolean };

export async function petManifest(): Promise<PetSkinItem[]> {
  const data = await jsonFetch<{ items: PetSkinItem[] }>("/pet-market/manifest");
  return data.items;
}

export async function petSearch(q: string, cat: string): Promise<PetSearchItem[]> {
  const data = await jsonFetch<{ items: PetSearchItem[] }>(
    `/pet-market/search?q=${encodeURIComponent(q)}&cat=${encodeURIComponent(cat)}`,
  );
  return data.items;
}

export async function petDetail(skin_id: string): Promise<PetSkinDetail> {
  return jsonFetch<PetSkinDetail>(`/pet-market/detail/${encodeURIComponent(skin_id)}`);
}

export async function petInstalled(): Promise<PetInstalledItem[]> {
  const data = await jsonFetch<{ items: PetInstalledItem[] }>("/pet-market/installed");
  return data.items;
}

export async function petApplied(): Promise<PetAppliedSkin> {
  return jsonFetch<PetAppliedSkin>("/pet-market/applied");
}

export async function petProgress(): Promise<PetProgress> {
  return jsonFetch<PetProgress>("/pet-market/progress");
}

export async function petInstall(skin_id: string): Promise<PetTaskItem> {
  return jsonFetch("/pet-market/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

export async function petCancel(skin_id: string): Promise<PetTaskItem> {
  return jsonFetch("/pet-market/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

export async function petApply(skin_id: string): Promise<{ applied: string }> {
  return jsonFetch("/pet-market/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

export async function petUninstall(skin_id: string): Promise<{ uninstalled: string; reset_applied?: boolean }> {
  return jsonFetch("/pet-market/uninstall", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

// ---- GitHub 扫描器（发现 Tab） ----

export type PetScanProgress = {
  status: string;      // idle | running | done | failed | cancelled
  phase: string;
  step: string;
  total: number;
  current: number;
  repos_seen: number;
  repos_lic_skip: number;
  repos_tree_skip: number;
  candidates: number;
  built_ok: number;
  built_fail: number;
  atlas_skip: number;
  error: string;
  finished_at: string;
  fails: Array<{ repo: string; path: string; reason: string }>;  // 试转失败原因日志
};
export type PetDiscoveryItem = {
  id: string;
  name: string;
  category?: string;
  license?: string;
  attribution?: string;
  description?: string;
  source_type?: string;
  installed: boolean;
  discovery: {
    repo: string;
    stars: number;
    license: string;
    kind: string;
    path: string;
    count: number;
    scanned_at: string;
  };
};

export async function petScanStart(): Promise<PetScanProgress> {
  return jsonFetch("/pet-market/scan", { method: "POST" });
}

export async function petScanProgress(): Promise<PetScanProgress> {
  return jsonFetch("/pet-market/scan/progress");
}

export async function petScanCancel(): Promise<PetScanProgress> {
  return jsonFetch("/pet-market/scan/cancel", { method: "POST" });
}

export async function petDiscovery(): Promise<PetDiscoveryItem[]> {
  const data = await jsonFetch<{ items: PetDiscoveryItem[] }>("/pet-market/discovery");
  return data.items;
}

export async function petDiscoveryRemove(skin_id: string): Promise<{ removed: string }> {
  return jsonFetch(`/pet-market/discovery/${encodeURIComponent(skin_id)}`, { method: "DELETE" });
}
