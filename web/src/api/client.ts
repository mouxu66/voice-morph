import type { ClipItem, DiagnoseInfo, HealthInfo, VideoItem, VoiceInfo, VoiceQc } from "../types";

// 后端统一挂在 /api 前缀下。
// 开发模式：走 vite proxy（/api -> 8000），用相对地址；
// 生产模式（electron 打包后 file:// 协议）：直接用绝对地址连本地后端。
// dev(vite 5173) 走代理；Electron(file://) 走本机 8000；手机/局域网(由后端 8000 托管页面)走同源
export const BASE = import.meta.env.DEV
  ? "/api"
  : typeof location !== "undefined" && (location.protocol === "http:" || location.protocol === "https:")
    ? "/api"
    : "http://127.0.0.1:8000/api";

// 把后端返回的相对音频路径（如 /media/outputs/x.wav）转成可播放的绝对地址。
// 生产环境后端返回的 url 是 /api/media/...，这里做兜底拼接。
export function mediaUrl(path: string): string {
  if (!path) return "";
  if (/^https?:\/\//.test(path)) return path;
  if (path.startsWith("/api/")) return import.meta.env.DEV ? path : "http://127.0.0.1:8000" + path;
  // 旧格式相对路径（/media/...），拼上后端前缀
  return BASE + path;
}

async function jsonFetch<T>(url: string, init?: RequestInit): Promise<T> {
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

export async function getHealth(): Promise<HealthInfo> {
  return jsonFetch<HealthInfo>("/health");
}

/** 环境体检：检查 ffmpeg / RVC 整合包 / 默认音色权重 / CUDA / TTS 模型 等本机依赖 */
export async function diagnose(): Promise<DiagnoseInfo> {
  return jsonFetch<DiagnoseInfo>("/diagnose");
}

export async function listVoices(): Promise<VoiceInfo[]> {
  const data = await jsonFetch<{ voices: VoiceInfo[] }>("/voices");
  return data.voices;
}

// ---- M1 素材流水线（后端需提供以下端点） ----

export async function listRawVideos(): Promise<VideoItem[]> {
  const data = await jsonFetch<{ videos: VideoItem[] }>("/raw_videos");
  return data.videos;
}

export async function deleteRawVideo(name: string, force: boolean): Promise<{ ok: boolean; clips: number; related: number }> {
  return jsonFetch(`/raw_videos/${encodeURIComponent(name)}?force=${force}`, { method: "DELETE" });
}

export type PipelineStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  step: string;
  message: string;
  percent: number;
  clips: number;
  error: string;
};

export async function runPipeline(): Promise<{ ok: boolean; started: boolean }> {
  return jsonFetch("/pipeline/run", { method: "POST" });
}

export async function getPipelineStatus(): Promise<PipelineStatus> {
  return jsonFetch<PipelineStatus>("/pipeline/status");
}

export async function cancelPipeline(): Promise<{ ok: boolean }> {
  return jsonFetch("/pipeline/cancel", { method: "POST" });
}

export async function listClips(): Promise<ClipItem[]> {
  const data = await jsonFetch<{ clips: ClipItem[] }>("/clips");
  return data.clips;
}

/** 上传视频素材到 media/raw_videos/（拖拽/选择） */
export async function uploadVideo(file: File, onProgress?: (p: number) => void): Promise<{ ok: boolean; name: string; size_mb: number }> {
  const form = new FormData();
  form.append("file", file);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", BASE + "/upload/video");
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
    }
    xhr.responseType = "json";
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(xhr.response as { ok: boolean; name: string; size_mb: number });
      } else {
        const detail = (xhr.response as { detail?: string } | null)?.detail || xhr.statusText;
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => reject(new Error("网络错误，后端服务是否已启动？"));
    xhr.send(form);
  });
}

/** 在系统文件管理器中打开素材/输出目录 */
export async function openFolder(kind: "raw_videos" | "clips" | "outputs" | "voicebank"): Promise<{ ok: boolean; path: string }> {
  return jsonFetch("/open/folder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind }),
  });
}

// ---- 文字转语音 ----

// 后端保存 wav 并返回 {url, duration_s}，URL 可持久化、可下载。
export async function sendTts(
  text: string,
  textLanguage: "zh" | "en",
  voiceId: string
): Promise<{ url: string; duration_s: number; voice_id: string }> {
  const res = await fetch(BASE + "/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, text_language: textLanguage, voice_id: voiceId }),
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
  return res.json() as Promise<{ url: string; duration_s: number; voice_id: string }>;
}

// ---- 有声书工作台（长文 / SRT 逐句合成拼接导出） ----

export type AudiobookSegment = {
  i: number;
  text: string;
  url: string;
  duration_s: number;
  failed: boolean;
};

export type AudiobookStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  mode: "" | "text" | "srt";
  voice_id: string;
  done: number;
  total: number;
  percent: number;
  current_text: string;
  url: string;
  duration_s: number;
  error: string;
  segments: AudiobookSegment[];
};

export async function runAudiobook(
  text: string,
  voiceId: string,
  gapMs: number
): Promise<{ ok: boolean; mode: string; total: number; voice_id: string }> {
  return jsonFetch("/audiobook/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, voice_id: voiceId, gap_ms: gapMs }),
  });
}

export async function getAudiobookStatus(): Promise<AudiobookStatus> {
  return jsonFetch("/audiobook/status");
}

export async function cancelAudiobook(): Promise<{ ok: boolean; message?: string }> {
  return jsonFetch("/audiobook/cancel", { method: "POST" });
}

// ---- 离线变声工作台（录音/音频 → RVC 离线转换 → 导出） ----

export type OfflineVcStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "error";
  message: string;
  voice_id: string;
  url: string;
  duration_s: number;
  error: string;
};

export async function runOfflineVc(
  file: File,
  voiceId: string,
  pitch: number,
  indexRate: number,
  denoise: boolean
): Promise<{ ok: boolean; voice_id: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("voice_id", voiceId);
  form.append("pitch", String(pitch));
  form.append("index_rate", String(indexRate));
  form.append("denoise", String(denoise));
  const res = await fetch(BASE + "/offlinevc/run", { method: "POST", body: form });
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

export async function getOfflineVcStatus(): Promise<OfflineVcStatus> {
  return jsonFetch("/offlinevc/status");
}

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

// ---- 音频设备配置（一键最优 / 自动恢复） ----
// 角色：0=Console(默认) 1=Multimedia 2=Communications

export type AudioStatus = {
  ok: boolean;
  render: { "0": string | null; "1": string | null; "2": string | null };
  capture: { "0": string | null; "1": string | null; "2": string | null };
};

export type AudioConfigResult = { ok: boolean; error?: string; [key: string]: unknown };

export async function getAudioStatus(): Promise<AudioStatus> {
  return jsonFetch<AudioStatus>("/audio/status");
}

export async function applyAudioConfig(): Promise<AudioConfigResult> {
  return jsonFetch<AudioConfigResult>("/audio/apply", { method: "POST" });
}

export async function restoreAudioConfig(): Promise<AudioConfigResult> {
  return jsonFetch<AudioConfigResult>("/audio/restore", { method: "POST" });
}

// ---- RVC 袋鼠训练集（Qwen3-TTS 批量生成） ----

export type RvcDatasetItem = {
  name: string;
  duration_s: number;
  size_kb: number;
};

export type RvcDatasetInfo = {
  items: RvcDatasetItem[];
  export_dir: string;
};

export type RvcGenStatus = {
  running: boolean;
  total: number;
  done: number;
  current: string;
  error: string;
};

export async function listRvcDataset(voiceId?: string): Promise<RvcDatasetInfo> {
  const qs = voiceId ? `?voice_id=${encodeURIComponent(voiceId)}` : "";
  return jsonFetch<RvcDatasetInfo>(`/rvc/dataset${qs}`);
}

export async function generateRvcDataset(voiceId: string): Promise<{ ok: boolean; started: boolean; total: number }> {
  return jsonFetch("/rvc/dataset/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id: voiceId }),
  });
}

export async function getRvcGenStatus(): Promise<RvcGenStatus> {
  return jsonFetch<RvcGenStatus>("/rvc/dataset/status");
}

export async function exportRvcDataset(voiceId?: string): Promise<{ ok: boolean; copied: number; dest: string }> {
  const qs = voiceId ? `?voice_id=${encodeURIComponent(voiceId)}` : "";
  return jsonFetch(`/rvc/dataset/export${qs}`, { method: "POST" });
}

// ---- 袋鼠 RVC 模型状态（训练完成后的只读展示） ----

export type RvcModelStatus = {
  trained: boolean;
  pth_exists: boolean;
  index_exists: boolean;
  dataset_count: number;
  weights_dir: string;
  dataset_dir: string;
};

export async function getRvcModel(expName?: string): Promise<RvcModelStatus> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch<RvcModelStatus>(`/rvc/model${qs}`);
}

// ---- RVC 可选音色清单（音色库档案 ∩ RVC 已训练模型） ----
// 实时页用它做音色选择：只有 model_ready 的音色才能直接变声，
// 其余的按 dataset_count / has_reference 提示"先生成语料""先训练"。

export type RvcVoice = {
  id: string;
  display_name: string;
  has_reference: boolean;
  pth_exists: boolean;
  index_exists: boolean;
  model_ready: boolean;
  dataset_count: number;
  trained_at: string;
  /** 音色入库自动质检结果（outputs/qc/<id>.json；未质检时为 null） */
  qc?: VoiceQc | null;
};

export type RvcVoicesInfo = {
  voices: RvcVoice[];
  active_exp: string;
  default_exp: string;
  rvc_root: string;
  rvc_ready: boolean;
};

export async function listRvcVoices(): Promise<RvcVoicesInfo> {
  return jsonFetch<RvcVoicesInfo>("/rvc/voices");
}

// ---- RVC 实时变声 / 训练 ----

export type RvcLiveStatus = {
  ok: boolean;
  exp: string;
  model_ok: boolean;
  model_detail: string | null;
  pth_exists: boolean;
  index_exists: boolean;
  dataset_count: number;
  live_running: boolean;
  audio_switched: boolean;
  last_error?: string;
  train_running: boolean;
  output_device: string;
  input_device: string;
};

export type RvcStartResult = {
  ok: boolean;
  already_running?: boolean;
  pid?: number;
  audio_switched?: boolean;
  output_device?: string;
  hint?: string;
};

export type RvcTrainStatus = {
  ok: boolean;
  exp: string;
  model_ok: boolean;
  model_detail: string;
  running: boolean;
  rc: number | null;
  done: boolean;
  error: string;
  stage: string;
  stage_index: number;
  total_stages: number;
  percent: number;
  message: string;
  log_tail: string[];
  dataset_count: number;
  log_dir: string;
};

export async function rvcLiveStatus(expName?: string): Promise<RvcLiveStatus> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch<RvcLiveStatus>(`/rvc/live/status${qs}`);
}

export async function rvcLiveStart(expName?: string): Promise<RvcStartResult> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch<RvcStartResult>(`/rvc/live/start${qs}`, { method: "POST" });
}

export async function rvcLiveStop(): Promise<{ ok: boolean; restored?: boolean; error?: string; note?: string }> {
  return jsonFetch("/rvc/live/stop", { method: "POST" })
}

export async function rvcLiveReset(): Promise<{ ok: boolean; reset?: boolean; error?: string }> {
  return jsonFetch("/rvc/live/reset", { method: "POST" })
}

export async function rvcTrainStatus(expName?: string): Promise<RvcTrainStatus> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch<RvcTrainStatus>(`/rvc/train/status${qs}`);
}

export async function rvcTrainStart(opts?: { expName?: string; epochs?: number }): Promise<{
  ok: boolean;
  started?: boolean;
  already_running?: boolean;
  pid?: number;
}> {
  return jsonFetch("/rvc/train/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ exp_name: opts?.expName, epochs: opts?.epochs }),
  });
}

// ---- 级联变声（录音 → ASR → 文字 → TTS → 虚拟声卡） ----
// 走文字中转：源说话人的口音/发音习惯不进入输出，只保留目标音色。
// 与 RVC 实时变声互斥（同抢 GPU 与 CABLE 设备），后端会返回 409。

export type CascadeStage =
  | "idle" | "init" | "warming" | "capturing" | "asr" | "tts" | "playing" | "error";

export type CascadeStatus = {
  ok: boolean;
  running: boolean;
  pid: number | null;
  stage: CascadeStage | string;
  worker_ready: boolean;
  warming: boolean;
  audio_switched: boolean;
  last_error: string;
  last_text: string;
  last_asr_s: number;
  last_tts_s: number;
  avg_asr_s: number;
  p95_asr_s: number;
  avg_tts_s: number;
  p95_tts_s: number;
  last_audio_s: number;
  last_fast: boolean | null;
  chunks: number;
  dropped: number;
  avg_latency_s: number;
  last_latency_s: number;
  queued_s: number;
  input_device: string;
  output_device: string;
  child_error: string;
  updated_at: string;
};

export type CascadeStartResult = {
  ok: boolean;
  warming?: boolean;
  already_running?: boolean;
  pid?: number;
  output_device?: string;
  chunk_max_s?: number;
  mode?: string;
  hint?: string;
};

export async function cascadeStart(opts?: {
  voiceId?: string;
  chunkMaxS?: number;
  mode?: "stream" | "whole";
}): Promise<CascadeStartResult> {
  return jsonFetch<CascadeStartResult>("/cascade/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      voice_id: opts?.voiceId ?? null,
      chunk_max_s: opts?.mode === "whole" ? null : (opts?.chunkMaxS ?? null),
      mode: opts?.mode ?? "stream",
    }),
  });
}

export async function cascadeStop(): Promise<{ ok: boolean; restored?: boolean; error?: string; note?: string }> {
  return jsonFetch("/cascade/stop", { method: "POST" })
}

export async function cascadeStatus(): Promise<CascadeStatus> {
  return jsonFetch<CascadeStatus>("/cascade/status")
}

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

export async function ftTrain(voiceId: string, epochs = 12): Promise<{ ok: boolean; epochs: number }> {
  return jsonFetch(`/ft/train?voice_id=${encodeURIComponent(voiceId)}&epochs=${epochs}`, { method: "POST" });
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
