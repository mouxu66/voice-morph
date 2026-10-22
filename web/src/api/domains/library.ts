import { jsonFetch, BASE } from "../http";

// ---- 作品库（B1：收藏 / 标签 / 批量导出） ----

export type HistoryKind =
  | "tts"
  | "offlinevc"
  | "audiobook"
  | "fx"
  | "trial"
  | "mine"
  | "seedvc"
  | "cover";

export type HistoryItem = {
  id: string;
  ts: number;
  kind: HistoryKind;
  voice_id: string;
  wav: string;
  url: string;
  duration_s: number;
  input_text: string;
  params: Record<string, unknown>;
  starred: boolean;
  tags: string[];
};

export type HistoryQuery = {
  items: HistoryItem[];
  total: number;
  limit: number;
  offset: number;
};

export type TagCount = { tag: string; count: number };

export async function listHistory(opts: {
  kind?: string; voice_id?: string; starred?: boolean; tag?: string;
  from_ts?: number; to_ts?: number; limit?: number; offset?: number;
} = {}): Promise<HistoryQuery> {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(opts)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const qs = p.toString();
  return jsonFetch(qs ? `/history?${qs}` : "/history");
}

export async function historyTags(): Promise<TagCount[]> {
  const r = await jsonFetch<{ tags: TagCount[] }>("/history/tags");
  return r.tags;
}

export async function patchHistoryMeta(
  id: string, patch: { starred?: boolean; tags?: string[] },
): Promise<HistoryItem> {
  const r = await jsonFetch<{ ok: boolean; item: HistoryItem }>(`/history/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return r.item;
}

export async function bulkDeleteHistory(
  ids: string[], keepFile = false,
): Promise<{ ok: boolean; deleted: number; failed: { id: string; error: string }[] }> {
  return jsonFetch("/history/bulk_delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids, keep_file: keepFile }),
  });
}

/** 勾选作品打包下载：POST 拿到 zip blob 后触发浏览器保存 */
export async function exportHistoryZip(ids: string[]): Promise<{ missing: number }> {
  const res = await fetch(BASE + "/history/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
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
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `works-${new Date().toISOString().slice(0, 10)}.zip`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
  return { missing: Number(res.headers.get("X-Missing-Files") ?? 0) };
}
