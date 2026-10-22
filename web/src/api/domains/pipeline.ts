import type { ClipItem, VideoItem } from "../../types";
import { jsonFetch, BASE } from "../http";

// ---- M1 素材流水线（后端需提供以下端点） ----

export async function listRawVideos(): Promise<VideoItem[]> {
  const data = await jsonFetch<{ videos: VideoItem[] }>("/raw_videos");
  return data.videos;
}

export async function deleteRawVideo(name: string, force: boolean): Promise<{ ok: boolean; clips: number; related: number }> {
  return jsonFetch(`/raw_videos/${encodeURIComponent(name)}?force=${force}`, { method: "DELETE" });
}

/** P2-1 失败可诊断结论：由后端 `quality_verdict.build_quality_verdict()` 产出。
 *  `verdict` 是机器判定码，配色/文案查 `lib/qualityVerdict.ts` 的 VERDICT_META ——
 *  不要在这里再写一份 label（两份 label 必然对不上）。 */
export type PipelineVerdict = {
  verdict: string;
  title: string;
  detail: string;
  actions: string[];
  ok_ratio: number;
  total: number;
  ok: number;
  grades: { A: number; B: number; C: number; D: number };
  reason: string;
};

export type PipelineStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  step: string;
  message: string;
  percent: number;
  clips: number;
  error: string;
  /** 可能缺省（旧后端 / 未跑过质检）—— 前端一律当作「没有结论」处理 */
  verdict?: PipelineVerdict | null;
};

export async function runPipeline(files?: string[]): Promise<{ ok: boolean; started: boolean }> {
  const qs = files && files.length ? "?" + files.map((f) => `file=${encodeURIComponent(f)}`).join("&") : "";
  return jsonFetch(`/pipeline/run${qs}`, { method: "POST" });
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

// ---- 说话人分离（CAM++ diarization） ----

export type DiarSpeaker = {
  id: number;
  label: string;
  duration: number;
  segment_count: number;
  ratio: number;
  is_main: boolean;
};
export type DiarSegment = {
  start: number;
  end: number;
  duration: number;
  spk: number;
  label: string;
  is_main: boolean;
};
export type DiarClip = { name: string; spk: number | null };
export type DiarizeResult = {
  ok: boolean;
  analyzed: number;
  n_speakers: number;
  main_speaker: number;
  main_label: string;
  speakers: DiarSpeaker[];
  segments: DiarSegment[];
  clips: DiarClip[];
  model?: string;
};

/** 对某素材做说话人分离，返回说话人列表 + 每个切片的说话人标签（按主说话人推荐） */
export async function diarizeClips(file: string): Promise<DiarizeResult> {
  return jsonFetch<DiarizeResult>(`/clips/diarize?file=${encodeURIComponent(file)}`, { method: "POST" });
}

export type QcSummary = {
  ok: boolean;
  prefix: string;
  count: number;
  grades: { A: number; B: number; C: number; D: number };
  ok_count: number;
  has_spk: boolean;
  main_spk: number | null;
  updated_at: string;
};

/** 对某素材的切片做质检打分（spk=true 时会先跑一次说话人分离，较慢但能剔除他人声） */
export async function qcClips(file: string, spk = true, force = false): Promise<QcSummary> {
  return jsonFetch<QcSummary>(
    `/clips/qc?file=${encodeURIComponent(file)}&spk=${spk ? 1 : 0}&force=${force ? 1 : 0}`,
    { method: "POST" });
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
