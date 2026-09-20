// 变声工坊移动端 API 层 —— 对齐 m2_server FastAPI 后端（/api 前缀）
// 手机与 PC 需在同一局域网，后端默认监听 0.0.0.0:8000
import { useAppStore } from "./store";

export interface HealthInfo {
  status: string;
  cuda: boolean;
}

export interface VoiceInfo {
  id: string;
  display_name?: string;
  reference: string;
  duration_s: number;
  kind?: "clone" | "finetuned";
}

export interface RvcVoice {
  id: string;
  display_name: string;
  has_reference: boolean;
  pth_exists: boolean;
  index_exists: boolean;
  model_ready: boolean;
  dataset_count: number;
  trained_at: string;
  /** 训练完成自动质检结果（未跑或失败时为 null） */
  qc?: VoiceQc | null;
}

export interface RvcVoicesInfo {
  voices: RvcVoice[];
  active_exp: string;
  default_exp: string;
  rvc_root: string;
  rvc_ready: boolean;
}

export interface CascadeStatus {
  ok: boolean;
  running: boolean;
  pid: number | null;
  stage: string;
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
  chunks: number;
  dropped: number;
  avg_latency_s: number;
  queued_s: number;
  input_device: string;
  output_device: string;
  child_error: string;
  updated_at: string;
}

export interface RvcLiveStatus {
  ok: boolean;
  exp: string;
  model_ok: boolean;
  model_detail: string | null;
  live_running: boolean;
  audio_switched: boolean;
  train_running: boolean;
  output_device: string;
  input_device: string;
  // 实时转写字幕（--asr-only 子进程）：running/stage/last_text/chunks
  asr_running: boolean;
  asr_stage: string;
  asr_last_text: string;
  asr_chunks: number;
  asr_error: string;
  asr_updated_at: string;
}

/** RVC 训练进度（解析 train_run.log） */
export interface TrainStatus {
  ok: boolean;
  running: boolean;
  done: boolean;
  error: string;
  stage: string;
  stage_index: number;
  total_stages: number;
  percent: number;
  message: string;
  dataset_count: number;
  exp: string;
}

/** M1 素材流水线状态 */
export interface PipelineStatus {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  step: string;
  message: string;
  percent: number;
  clips: number;
  error: string;
}

/** 音色质检结果（outputs/qc/<exp>.json） */
export interface VoiceQc {
  score: number;
  pass: boolean;
  created_at?: string;
}

export interface OfflineVcStatus {
  running: boolean;
  status: "idle" | "running" | "done" | "error";
  message: string;
  voice_id: string;
  url: string;
  duration_s: number;
  error: string;
}

export interface TtsResult {
  url: string;
  duration_s: number;
  voice_id: string;
}

export function getHost(): string {
  return useAppStore.getState().host;
}

export function getApiKey(): string {
  return useAppStore.getState().apiKey;
}

/** 后端返回的相对音频路径 → 手机可播放的绝对地址。
 *  原生播放器请求 URL 时带不了自定义 header，鉴权走 api_key 查询参数。 */
export function mediaUrl(path: string): string {
  const host = getHost();
  const key = getApiKey();
  if (!path) return "";
  let url: string;
  if (/^https?:\/\//.test(path)) return path;
  if (path.startsWith("/api/")) url = host + path;
  else url = host + "/api" + path;
  if (!key) return url;
  return url + (url.includes("?") ? "&" : "?") + "api_key=" + encodeURIComponent(key);
}

async function jsonFetch<T>(path: string, init?: RequestInit, timeoutMs = 10000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  try {
    const res = await fetch(getHost() + "/api" + path, { ...init, headers, signal: ctrl.signal });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

// ---- 基础 ----

export async function getHealth(): Promise<HealthInfo> {
  return jsonFetch<HealthInfo>("/health", undefined, 5000);
}

export async function listVoices(): Promise<VoiceInfo[]> {
  const data = await jsonFetch<{ voices: VoiceInfo[] }>("/voices");
  return data.voices;
}

export async function listRvcVoices(): Promise<RvcVoicesInfo> {
  return jsonFetch<RvcVoicesInfo>("/rvc/voices");
}

// ---- 能力清单（插件目录，GET /api/plugins）----

export interface PluginEntry {
  id: string;
  name?: string;
  kind?: string;
  category?: string;
  order?: number;
  summary?: string;
  core?: boolean;
  state?: string;
  reasons?: string[];
  enabled?: boolean;
  routers?: string[];
}

export interface PluginCatalog {
  ok?: boolean;
  counts?: { total: number; ok: number; broken: number; disabled: number };
  plugins: PluginEntry[];
}

/** 拉一次能力清单；后端在 8000 端口，经 getHost() 拼地址。 */
export async function getPlugins(): Promise<PluginCatalog> {
  return jsonFetch<PluginCatalog>("/plugins", undefined, 6000);
}

// ---- 级联变声（遥控 PC 端运行） ----

export async function cascadeStart(opts?: {
  voiceId?: string | null;
  chunkMaxS?: number | null;
  mode?: "stream" | "whole";
}): Promise<{ ok: boolean; warming?: boolean; already_running?: boolean; hint?: string }> {
  return jsonFetch("/cascade/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      voice_id: opts?.voiceId ?? null,
      chunk_max_s: opts?.mode === "whole" ? null : (opts?.chunkMaxS ?? null),
      mode: opts?.mode ?? "stream",
    }),
  });
}

export async function cascadeStop(): Promise<{ ok: boolean; restored?: boolean; error?: string }> {
  return jsonFetch("/cascade/stop", { method: "POST" });
}

export async function cascadeStatus(): Promise<CascadeStatus> {
  return jsonFetch<CascadeStatus>("/cascade/status", undefined, 6000);
}

// ---- RVC 实时变声（遥控 PC 端运行） ----

export async function rvcLiveStart(expName?: string): Promise<{ ok: boolean; already_running?: boolean; hint?: string }> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch(`/rvc/live/start${qs}`, { method: "POST" });
}

export async function rvcLiveStop(): Promise<{ ok: boolean; restored?: boolean; error?: string }> {
  return jsonFetch("/rvc/live/stop", { method: "POST" });
}

// ---- PC 状态镜像（训练进度 / 素材流水线，只读监控） ----

export async function getTrainStatus(expName?: string): Promise<TrainStatus> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch<TrainStatus>(`/rvc/train/status${qs}`);
}

export async function getPipelineStatus(): Promise<PipelineStatus> {
  return jsonFetch<PipelineStatus>("/pipeline/status");
}

export async function rvcLiveStatus(expName?: string): Promise<RvcLiveStatus> {
  const qs = expName ? `?exp_name=${encodeURIComponent(expName)}` : "";
  return jsonFetch<RvcLiveStatus>(`/rvc/live/status${qs}`, undefined, 6000);
}

// ---- 离线变声（手机录音上传 → PC 转换 → 回传播放） ----

export async function runOfflineVc(
  fileUri: string,
  voiceId: string,
  pitch: number,
  indexRate: number,
  denoise: boolean
): Promise<{ ok: boolean; voice_id: string }> {
  const form = new FormData();
  // RN FormData：文件用 {uri, name, type}。扩展名取录音真实产出（expo-av 高质量预设是 m4a），
  // 不能写死 .wav——后端按后缀决定处理方式，名不符实会埋雷
  const ext = (fileUri.split(".").pop() || "m4a").toLowerCase().replace(/[^a-z0-9]/g, "") || "m4a";
  const mime = ext === "wav" ? "audio/wav" : ext === "mp4" || ext === "m4a" ? "audio/mp4" : "application/octet-stream";
  form.append("file", { uri: fileUri, name: `record.${ext}`, type: mime } as unknown as Blob);
  form.append("voice_id", voiceId);
  form.append("pitch", String(pitch));
  form.append("index_rate", String(indexRate));
  form.append("denoise", String(denoise));
  return jsonFetch("/offlinevc/run", { method: "POST", body: form }, 120000);
}

export async function getOfflineVcStatus(): Promise<OfflineVcStatus> {
  return jsonFetch<OfflineVcStatus>("/offlinevc/status");
}

// ---- TTS 克隆合成 ----

export async function sendTts(text: string, textLanguage: "zh" | "en", voiceId: string): Promise<TtsResult> {
  return jsonFetch<TtsResult>(
    "/tts",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, text_language: textLanguage, voice_id: voiceId }),
    },
    120000
  );
}

// ---- 音频设备（PC 端声卡一键最优/恢复） ----

export async function applyAudioConfig(): Promise<{ ok: boolean; error?: string }> {
  return jsonFetch("/audio/apply", { method: "POST" }, 30000);
}

export async function restoreAudioConfig(): Promise<{ ok: boolean; error?: string }> {
  return jsonFetch("/audio/restore", { method: "POST" }, 30000);
}

// ---- 微信语音发送（遥控 PC 端把合成语音灌进 PC 微信语音条） ----

export interface WechatHistoryItem {
  wav: string;
  duration_s: number;
  ts: number;
  outcome: string;
}

export interface WechatSendResult {
  ok: boolean;
  wav?: string;
  duration_s?: number;
  outcome?: string;
  error?: string;
}

/**
 * 遥控 PC 全自动发送：PC 端切录音设备 → 前台化微信模拟按住 Alt → 播放 → 松开发送。
 * 执行期间（约 wav 时长 + 3s）PC 键鼠会被接管。wav=PC outputs/ 下文件名，缺省=PC 最近 TTS 产物。
 */
export async function wechatSendVoice(wav?: string): Promise<WechatSendResult> {
  return jsonFetch(
    "/wechat/send_voice",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wav: wav || null }),
    },
    120000
  );
}

export async function getWechatHistory(): Promise<{ ok: boolean; items: WechatHistoryItem[] }> {
  return jsonFetch<{ ok: boolean; items: WechatHistoryItem[] }>("/wechat/history");
}
