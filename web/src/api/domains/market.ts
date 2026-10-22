import { jsonFetch } from "../http";

// ---- 音色市场（双源搜索 / 推荐清单 / 一键安装） ----

export type MarketPlatform = "hf" | "modelscope";

export type MarketFileSlot = {
  url: string;
  mirror_url?: string | null;
  sha256?: string | null;
};

export type MarketFile = {
  name: string;
  path: string;
  size: number;
  type: string;
  url?: string | null;
  mirror_url?: string | null;
  sha256?: string | null;
};

export type MarketItem = {
  id: string;
  voice_id?: string;
  name: string;
  platform: MarketPlatform;
  repo: string;
  category?: string;
  desc?: string;
  /** 精选条目可选配图 URL（本地 /media 托管或远程直链）；无图时前端用分类色首字母占位 */
  image?: string;
  size_hint_mb?: number;
  license?: string;
  demo?: string;
  downloads?: number;
  likes?: number;
  updated_at?: string;
  tags?: string[];
  /** 后端汉化的中文标签（英文元数据 → 可读介绍），无则回退 tags */
  tags_zh?: string[];
  files?: MarketFile[];
  download?: MarketFileSlot;
  index?: MarketFileSlot | null;
  /** 魔搭降级搜索附带的清单原始条目（含 download/index 直链） */
  prefs?: MarketItem;
};

export type MarketInstallState = {
  voice_id: string;
  display_name: string;
  manifest_id?: string;
  status: string;
  phase: string;
  message: string;
  percent: number;
  error: string;
  started_at?: string;
  updated_at?: string;
  resume_pth?: boolean;
};

export type MarketTask = {
  name?: string;
  filename?: string;
  status?: string;
  total?: number | null;
  done?: number;
  started_at?: string;
  error?: string;
  install?: MarketInstallState;
};

export async function marketManifest(): Promise<MarketItem[]> {
  const data = await jsonFetch<{ items: MarketItem[] }>("/market/manifest");
  return data.items;
}

export async function marketSearch(
  q: string,
  platform: string = "all",
  limit: number = 10,
  skip: number = 0,
): Promise<{ items: MarketItem[]; note: string | null; next_skip: number | null }> {
  const qs = `q=${encodeURIComponent(q)}&platform=${encodeURIComponent(platform)}&limit=${limit}&skip=${skip}`;
  return jsonFetch(`/market/search?${qs}`);
}

export async function marketRepo(
  repo: string,
  platform: MarketPlatform | string = "hf",
  recursive: boolean = false,
): Promise<{ repo: string; platform: string; files: MarketFile[]; readme?: string | null }> {
  const qs = `repo=${encodeURIComponent(repo)}&platform=${encodeURIComponent(platform)}&recursive=${recursive}`;
  return jsonFetch(`/market/repo?${qs}`);
}

export async function marketInstall(req: {
  voice_id: string;
  download: MarketFileSlot;
  index?: MarketFileSlot | null;
  display_name?: string;
  manifest_id?: string;
  overwrite?: boolean;
}): Promise<{ task: MarketTask }> {
  return jsonFetch("/market/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function marketProgress(): Promise<{ task: MarketTask | null }> {
  return jsonFetch("/market/progress");
}

export async function marketInstalled(): Promise<string[]> {
  const data = await jsonFetch<{ installed: string[] }>("/market/installed");
  return data.installed;
}

export async function marketCancel(): Promise<{ task: MarketTask | null }> {
  return jsonFetch("/market/cancel", { method: "POST" });
}

export async function marketUninstall(voice_id: string): Promise<{ voice_id: string; removed: string[] }> {
  return jsonFetch("/market/uninstall", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id }),
  });
}

/** 有历史备份（可回滚）的市场音色 id 列表 */
export async function marketBackups(): Promise<string[]> {
  const data = await jsonFetch<{ backups: string[] }>("/market/backups");
  return data.backups;
}

/** 回滚到上次覆盖前的版本（消费最新 .old 备份） */
export async function marketRollback(voice_id: string): Promise<{ voice_id: string }> {
  return jsonFetch("/market/rollback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id }),
  });
}

// ---- 市场试听（A2：装完自动生成固定句试听） ----

export type MarketPreviewStatus = "ready" | "generating" | "failed" | "skipped" | "missing";
export type MarketPreview = { status: MarketPreviewStatus; url: string; error: string };

export async function marketPreviewStatus(voice_id: string): Promise<MarketPreview> {
  return jsonFetch(`/market/preview?voice_id=${encodeURIComponent(voice_id)}`);
}

export async function marketPreviewTrigger(
  voice_id: string,
  download?: MarketFileSlot | null,
): Promise<MarketPreview> {
  return jsonFetch("/market/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id, download: download ?? undefined }),
  });
}
