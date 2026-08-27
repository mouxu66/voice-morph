import type { ClipItem, HealthInfo, VideoItem, VoiceInfo } from "../types";

// 后端统一挂在 /api 前缀下。
// 开发模式：走 vite proxy（/api -> 8000），用相对地址；
// 生产模式（electron 打包后 file:// 协议）：直接用绝对地址连本地后端。
export const BASE = import.meta.env.DEV ? "/api" : "http://127.0.0.1:8000/api";

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

export async function listVoices(): Promise<VoiceInfo[]> {
  const data = await jsonFetch<{ voices: VoiceInfo[] }>("/voices");
  return data.voices;
}

// ---- M1 素材流水线（后端需提供以下端点） ----

export async function listRawVideos(): Promise<VideoItem[]> {
  const data = await jsonFetch<{ videos: VideoItem[] }>("/raw_videos");
  return data.videos;
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
  kept: number;
  clusters: MineCluster[];
};

export async function mineRun(): Promise<{ ok: boolean; already_running?: boolean }> {
  return jsonFetch("/mine/run", { method: "POST" });
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

export async function listRvcDataset(): Promise<RvcDatasetInfo> {
  return jsonFetch<RvcDatasetInfo>("/rvc/dataset");
}

export async function generateRvcDataset(): Promise<{ ok: boolean; started: boolean; total: number }> {
  return jsonFetch("/rvc/dataset/generate", { method: "POST" });
}

export async function getRvcGenStatus(): Promise<RvcGenStatus> {
  return jsonFetch<RvcGenStatus>("/rvc/dataset/status");
}

export async function exportRvcDataset(): Promise<{ ok: boolean; copied: number; dest: string }> {
  return jsonFetch("/rvc/dataset/export", { method: "POST" });
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

export async function getRvcModel(): Promise<RvcModelStatus> {
  return jsonFetch<RvcModelStatus>("/rvc/model");
}

// ---- RVC 实时变声 / 训练 ----

export type RvcLiveStatus = {
  ok: boolean;
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

export async function rvcLiveStatus(): Promise<RvcLiveStatus> {
  return jsonFetch<RvcLiveStatus>("/rvc/live/status");
}

export async function rvcLiveStart(): Promise<RvcStartResult> {
  return jsonFetch<RvcStartResult>("/rvc/live/start", { method: "POST" });
}

export async function rvcLiveStop(): Promise<{ ok: boolean; restored?: boolean; error?: string; note?: string }> {
  return jsonFetch("/rvc/live/stop", { method: "POST" })
}

export async function rvcLiveReset(): Promise<{ ok: boolean; reset?: boolean; error?: string }> {
  return jsonFetch("/rvc/live/reset", { method: "POST" })
}

export async function rvcTrainStatus(): Promise<RvcTrainStatus> {
  return jsonFetch<RvcTrainStatus>("/rvc/train/status");
}

export async function rvcTrainStart(): Promise<{ ok: boolean; started?: boolean; already_running?: boolean; pid?: number }> {
  return jsonFetch("/rvc/train/start", { method: "POST" });
}
