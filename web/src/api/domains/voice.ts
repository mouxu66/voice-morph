import { jsonFetch, BASE } from "../http";

// ---- 音色挖掘（解析切片 → 自动筛音色 → 迭代试听 → 保存） ----

export type MineCluster = {
  cluster: number;
  size: number;
  members: string[];
  rep: { name: string; path: string; text: string };
};

export type MineState = {
  running: boolean;
  stage: "idle" | "running" | "done" | "error";
  message: string;
  /** 挖掘过程中逐条切片的失败原因（后端会写入但此前前端未展示） */
  errors?: string[];
  kept: number;
  clusters: MineCluster[];
};

export async function mineRun(params?: {
  sim_threshold?: number
  min_cluster_size?: number
}): Promise<{ ok: boolean; already_running?: boolean }> {
  return jsonFetch("/mine/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params ?? {}),
  });
}

export async function getMineState(): Promise<MineState> {
  return jsonFetch<MineState>("/mine/state");
}

export async function minePreview(
  clip: string,
  text = ""
): Promise<{ url: string; text: string; ref_text: string; duration_s: number }> {
  return jsonFetch("/mine/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip, text }),
  });
}

export async function mineSave(
  clip: string,
  voiceId: string,
  displayName: string,
  members: string[] = []
): Promise<{ ok: boolean; voice_id: string; duration_s: number; clips: number }> {
  return jsonFetch("/mine/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clip, voice_id: voiceId, display_name: displayName, members }),
  });
}

// ---- 音色包导出 / 导入 ----

/** 音色包下载地址（<a href> 直接下载；includeRvc 附带 RVC 权重，可在别处直接实时变声） */
export function voicePackUrl(voiceId: string, includeRvc = false): string {
  return `${BASE}/voicebank/${encodeURIComponent(voiceId)}/export?include_rvc=${includeRvc}`;
}

export async function importVoicePack(
  file: File,
  overwrite = false
): Promise<{ ok: boolean; voice_id: string; kind: string; rvc_files: number; display_name: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/voicebank/import?overwrite=${overwrite}`, { method: "POST", body: form });
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
  return res.json();
}
