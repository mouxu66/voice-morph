import { jsonFetch, BASE } from "../http";

// ---- 音色微调工坊（录音 → 切片转写 → 少样本微调 → 试听 → 入库） ----

export type FtStatus = {
  stage: "new" | "processing" | "ready" | "training" | "trained" | "published" | "error";
  voice_id: string;
  message?: string;
  error?: string;
  clips?: number;
  duration_s?: number;
  speech_s?: number;
  anchor?: string;
  transcripts?: { name: string; text: string; quality: number }[];
  train?: { pid: number | null; running: boolean; rc: number | null };
};

export type FtTrainStatus = {
  stage: string;
  message: string;
  error: string;
  running: boolean;
  rc: number | null;
  checkpoint: string | null;
  log_tail: string[];
  loss: number | null;
  epoch: number | null;
  epochs: number | null;
  vram_peak: number | null;
  done?: boolean;
};

/** 微调语料体检报告（A3：训练前的语料质量闸门） */
export type FtCorpusQc = {
  voice_id: string;
  updated_at: string;
  count: number;
  has_spk: boolean;
  grades: { A: number; B: number; C: number; D: number };
  ok_count: number;
  avg_score: number;
  total_s: number;
  rejected_count: number;
  top_reasons: { reason: string; count: number }[];
  advice: string[];
  clips: Record<string, {
    name: string; score: number; grade: "A" | "B" | "C" | "D";
    reasons: string[]; duration_s: number; spk_sim: number | null;
  }>;
};

export type FtPruneResult = {
  ok: boolean;
  kept: number;
  moved: number;
  moved_names: string[];
  speech_s: number;
  rejected: number;
  warning: string;
};

export type FtRestoreResult = {
  ok: boolean;
  restored: number;
  rows: number;
  speech_s: number;
  rejected: number;
  grades: { A: number; B: number; C: number; D: number };
};

export async function getFtCorpusQc(
  voiceId: string,
  opts?: { withSpk?: boolean; force?: boolean },
): Promise<FtCorpusQc> {
  const qs = `voice_id=${encodeURIComponent(voiceId)}`
    + `&with_spk=${opts?.withSpk ? "true" : "false"}`
    + `&force=${opts?.force ? "true" : "false"}`;
  return jsonFetch<FtCorpusQc>(`/ft/corpus_qc?${qs}`);
}

export async function ftCorpusPrune(
  voiceId: string,
  opts?: { keepGrades?: string; minScore?: number },
): Promise<FtPruneResult> {
  let qs = `voice_id=${encodeURIComponent(voiceId)}`;
  if (opts?.keepGrades) qs += `&keep_grades=${encodeURIComponent(opts.keepGrades)}`;
  if (typeof opts?.minScore === "number") qs += `&min_score=${opts.minScore}`;
  return jsonFetch<FtPruneResult>(`/ft/corpus_prune?${qs}`, { method: "POST" });
}

export async function ftCorpusRestore(voiceId: string): Promise<FtRestoreResult> {
  return jsonFetch<FtRestoreResult>(
    `/ft/corpus_restore?voice_id=${encodeURIComponent(voiceId)}`, { method: "POST" });
}

export async function ftUpload(voiceId: string, file: Blob, filename: string): Promise<{ ok: boolean; voice_id: string }> {
  const form = new FormData();
  form.append("voice_id", voiceId);
  form.append("file", file, filename);
  const res = await fetch(BASE + "/ft/upload", { method: "POST", body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try { const b = await res.json(); if (b?.detail) detail = b.detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

export async function getFtStatus(voiceId: string): Promise<FtStatus> {
  return jsonFetch<FtStatus>(`/ft/status?voice_id=${encodeURIComponent(voiceId)}`);
}

export async function ftTrain(voiceId: string, epochs = 12, initFrom = ""): Promise<{
  ok: boolean;
  epochs: number;
  init_from?: string;
  qc?: { count: number; grades: { A: number; B: number; C: number; D: number }; ok_count: number; avg_score: number };
  qc_warning?: string;
}> {
  const initQ = initFrom ? `&init_from=${encodeURIComponent(initFrom)}` : "";
  return jsonFetch(`/ft/train?voice_id=${encodeURIComponent(voiceId)}&epochs=${epochs}${initQ}`, { method: "POST" });
}

export async function getFtTrainStatus(voiceId: string): Promise<FtTrainStatus> {
  return jsonFetch<FtTrainStatus>(`/ft/train_status?voice_id=${encodeURIComponent(voiceId)}`);
}

export async function ftAudition(voiceId: string, text: string): Promise<{ tuned_url: string; xvec_url?: string }> {
  return jsonFetch(`/ft/audition?voice_id=${encodeURIComponent(voiceId)}&text=${encodeURIComponent(text)}`, { method: "POST" });
}

export async function ftPublish(voiceId: string, displayName: string): Promise<{ ok: boolean; voice_id: string }> {
  return jsonFetch(`/ft/publish?voice_id=${encodeURIComponent(voiceId)}&display_name=${encodeURIComponent(displayName)}`, { method: "POST" });
}

export async function ftDelete(voiceId: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/ft/${encodeURIComponent(voiceId)}`, { method: "DELETE" });
}

// ---- A/B 音色对比（盲听 + 声纹相似度评分） ----

export type AbSide = { voice_id: string; url: string; similarity: number };

export type AbResult = {
  ok: boolean;
  text: string;
  A: AbSide;
  B: AbSide;
};

export async function abRun(voiceA: string, voiceB: string, text: string): Promise<AbResult> {
  return jsonFetch("/ab/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_a: voiceA, voice_b: voiceB, text }),
  });
}

// ---- 多链路对比评测（A4：同一输入 × RVC/Seed-VC/Qwen3 + 客观分） ----

export type ChainMetrics = { secs: number; nats: number; duration_s: number };

export type ChainLink = {
  status: "done" | "failed" | "skipped";
  url: string;
  error: string;
  metrics: ChainMetrics | null;
};

export type AbChainResult = {
  ok: boolean;
  target_voice_id: string;
  text: string;
  chains: { rvc: ChainLink; seed_vc: ChainLink; qwen3: ChainLink };
};

export async function abChainRun(file: File, voiceId: string, text: string): Promise<AbChainResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("voice_id", voiceId);
  if (text.trim()) form.append("text", text);
  const res = await fetch(BASE + "/ab/chain", { method: "POST", body: form });
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
