import { jsonFetch, BASE } from "../http";

// ---- 盲听评测（sound.blindtest） ----

export type BlindtestSessionSummary = {
  id: string;
  title: string;
  created_at: number;
  closed: boolean;
  n_samples: number;
  n_responses: number;
};

export type BlindtestSampleRow = {
  sid: string;
  label: string;
  order: number;
  n: number;
  mean: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
  rank?: number;
};

export type BlindtestResponse = {
  at: number;
  ua: string;
  scores: Record<string, number>;
  note: string;
};

export type BlindtestDetail = {
  id: string;
  title: string;
  created_at: number;
  closed: boolean;
  token: string;
  samples: { sid: string; label: string; order: number }[];
  aggregate: BlindtestSampleRow[];
  responses: BlindtestResponse[];
};

export async function blindtestSessions(): Promise<{ sessions: BlindtestSessionSummary[] }> {
  return jsonFetch("/blindtest/sessions");
}

/** 建盲听会话：上传样本（可混 outputs/ 相对路径引用），labels 与样本一一对应。 */
export async function blindtestCreate(
  title: string,
  labels: string[],
  files: File[],
  refs: string[]
): Promise<{ ok: boolean; id: string; token: string }> {
  const form = new FormData();
  form.append("title", title);
  form.append("labels", JSON.stringify(labels));
  form.append("refs", JSON.stringify(refs));
  for (const f of files) form.append("files", f);
  const res = await fetch(BASE + "/blindtest/sessions", { method: "POST", body: form });
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
  return res.json() as Promise<{ ok: boolean; id: string; token: string }>;
}

export async function blindtestDetail(id: string): Promise<BlindtestDetail> {
  return jsonFetch(`/blindtest/sessions/${encodeURIComponent(id)}`);
}

export async function blindtestClose(id: string, closed: boolean): Promise<{ ok: boolean }> {
  return jsonFetch(`/blindtest/sessions/${encodeURIComponent(id)}/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ closed }),
  });
}

export async function blindtestDelete(id: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/blindtest/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** 试音间当前任务的已完成结果（导入盲听样本用；试音间关着/没跑过 → 抛错由调用方兜）。 */
export type AuditionPick = { voice_id: string; display_name: string; url: string };

export async function auditionPicks(): Promise<AuditionPick[]> {
  const task = await jsonFetch<{
    results: { status: string; voice_id: string; display_name: string; url: string }[];
  }>("/audition/task");
  return (task.results || [])
    .filter((r) => r.status === "done" && r.url)
    .map((r) => ({ voice_id: r.voice_id, display_name: r.display_name, url: r.url }));
}
