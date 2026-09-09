import type { ClipItem, DiagnoseInfo, HealthInfo, SendChainInfo, VideoItem, VoiceInfo, VoiceQc } from "../types";

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

// ---- 文字转语音 ----

// 后端保存 wav 并返回 {url, duration_s}，URL 可持久化、可下载。
// styleRefVoice/segChars：风格参考 ICL + 长文分段（styleRefVoice=用该音色 reference 作风格参考）
export async function sendTts(
  text: string,
  textLanguage: "zh" | "en",
  voiceId: string,
  styleRefVoice?: string,
  segChars?: number
): Promise<{ url: string; duration_s: number; voice_id: string }> {
  const payload: Record<string, string | number> = {
    text,
    text_language: textLanguage,
    voice_id: voiceId,
  };
  if (styleRefVoice) payload.style_ref_voice = styleRefVoice;
  if (segChars && segChars > 0) payload.seg_chars = segChars;
  const res = await fetch(BASE + "/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
  denoise: boolean,
  postSeedVc = false,
  /** 降噪强度：light=轻·保弱声 standard=标准 strong=强力（不限压制） */
  enhanceLevel = "standard",
  /** 语气来源：keep=保留源音频语气（RVC 默认） relay=重铸语气（ASR→TTS→RVC） */
  prosody: "keep" | "relay" = "keep"
): Promise<{ ok: boolean; voice_id: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("voice_id", voiceId);
  form.append("pitch", String(pitch));
  form.append("index_rate", String(indexRate));
  form.append("denoise", String(denoise));
  form.append("post_seedvc", String(postSeedVc));
  form.append("enhance_level", enhanceLevel);
  form.append("prosody", prosody);
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

/** 自动音高建议：分析输入中位基频，对照目标音色参考音高，算建议变调（半音） */
export type PitchSuggestion = {
  input_f0: number | null;
  voiced_ratio: number;
  ref_f0: number | null;
  suggested_pitch: number | null;
  reliable: boolean;
};

export async function suggestPitch(file: File, voiceId: string): Promise<PitchSuggestion> {
  const form = new FormData();
  form.append("file", file);
  form.append("voice_id", voiceId);
  const res = await fetch(BASE + "/offlinevc/pitch_suggest", { method: "POST", body: form });
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
  return res.json() as Promise<PitchSuggestion>;
}

// ---- Seed-VC 表达力变声（零样本换声，保留/转换语气情绪，补 RVC 缺的表达力） ----

export type SeedVcStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "error";
  message: string;
  target: string;
  url: string;
  duration_s: number;
  error: string;
};

export async function runSeedVc(opts: {
  file: File;
  /** 目标音色：voicebank 音色 id 或上传参考音频，二选一 */
  targetVoiceId?: string;
  targetFile?: File;
  /** 开启情绪/口音转换（--convert-style） */
  convertStyle: boolean;
  similarityCfgRate: number;
  topP: number;
  temperature: number;
  diffusionSteps: number;
  lengthAdjust: number;
  denoise: boolean;
}): Promise<{ ok: boolean; target: string }> {
  const form = new FormData();
  form.append("file", opts.file);
  if (opts.targetFile) form.append("target", opts.targetFile);
  if (opts.targetVoiceId) form.append("target_voice_id", opts.targetVoiceId);
  form.append("convert_style", String(opts.convertStyle));
  form.append("similarity_cfg_rate", String(opts.similarityCfgRate));
  form.append("top_p", String(opts.topP));
  form.append("temperature", String(opts.temperature));
  form.append("diffusion_steps", String(opts.diffusionSteps));
  form.append("length_adjust", String(opts.lengthAdjust));
  form.append("denoise", String(opts.denoise));
  const res = await fetch(BASE + "/seedvc/run", { method: "POST", body: form });
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

export async function getSeedVcStatus(): Promise<SeedVcStatus> {
  return jsonFetch("/seedvc/status");
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

/** 发送链路自检（A1）：只读检查虚拟声卡/默认设备/备份残留，items 与 /diagnose 同构。 */
export async function sendChainCheck(): Promise<SendChainInfo> {
  return jsonFetch<SendChainInfo>("/audio/send_chain");
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
  /** 来源标记：market=市场安装；自训/本地导入无标记 */
  source?: string;
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
  /** 进程活着但模型还没加载完时为 false，前端显示「加载中」 */
  live_ready?: boolean;
  /** 无头后台模式（不弹 RVC 窗口） */
  headless?: boolean;
  /** 自我监听回环是否在跑（能听到自己的变声） */
  monitor_on?: boolean;
  monitor_gain?: number | null;
  asr_running?: boolean;
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
  input_device?: string;
  headless?: boolean;
  monitor?: boolean;
  monitor_gain?: number | null;
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

/** 开关自我监听（变声运行中可随时调，on=false 时 gain 可省略） */
export async function rvcLiveMonitor(on: boolean, gain?: number): Promise<{ ok: boolean; monitor_on: boolean; monitor_gain?: number | null }> {
  const qs = new URLSearchParams({ on: String(on) });
  if (gain !== undefined) qs.set("gain", String(gain));
  return jsonFetch(`/rvc/live/monitor?${qs}`, { method: "POST" });
}

export async function rvcLiveReset(): Promise<{ ok: boolean; reset?: boolean; error?: string }> {
  return jsonFetch("/rvc/live/reset", { method: "POST" })
}

// ---- A7 实时输入设备选择 / A8 输入降噪 ----

export type LiveAudioDevice = {
  name: string;
  is_default: boolean;
};

export type LiveAudioDevices = {
  ok: boolean;
  items: LiveAudioDevice[];
  /** 用户显式选择的设备关键词（空串 = 跟随系统默认录音设备） */
  explicit: string;
  /** 上次变声实际用的输入设备（RVC config.json），未启动过为 null */
  current: string | null;
  denoise: boolean;
  running: boolean;
};

export async function getLiveAudioDevices(): Promise<LiveAudioDevices> {
  return jsonFetch<LiveAudioDevices>("/rvc/live/audio_devices");
}

/** input_device 传空串 = 跟随系统默认；变声运行中修改需重启变声生效（needs_restart） */
export async function setLiveAudioDevices(payload: { input_device?: string; denoise?: boolean }): Promise<{
  ok: boolean;
  input_device: string;
  denoise: boolean;
  running: boolean;
  needs_restart: boolean;
}> {
  return jsonFetch("/rvc/live/audio_devices", { method: "POST", body: JSON.stringify(payload) });
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
  /** 末尾接 RVC 时的音色归属；空串=未启用（音色靠 TTS 克隆） */
  rvc_voice?: string;
  rvc_error?: string;
  last_rvc_s?: number;
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
  /** 末尾接 RVC 的音色 ID：文字仍由 TTS 说，音色改由 RVC 替换（空=不接） */
  rvcVoice?: string;
}): Promise<CascadeStartResult> {
  return jsonFetch<CascadeStartResult>("/cascade/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      voice_id: opts?.voiceId ?? null,
      chunk_max_s: opts?.mode === "whole" ? null : (opts?.chunkMaxS ?? null),
      mode: opts?.mode ?? "stream",
      rvc_voice: opts?.rvcVoice ?? null,
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

// ---- 微信语音发送（PC 微信 4.1.9+ 虚拟声卡自动灌入） ----

export type WechatLastTts = {
  ok: boolean;
  wav?: string;
  duration_s?: number;
  url?: string;
  error?: string;
};

export type WechatHistoryItem = {
  wav: string;
  duration_s: number;
  ts: number;
  outcome: string;
};

export type WechatSendResult = {
  ok: boolean;
  wav?: string;
  duration_s?: number;
  outcome?: string;
  error?: string;
  steps?: string[];
  hint?: string;
  hint2?: string;
  warn?: string;
};

/** 最近一次 TTS 合成产物（发送前预览用） */
export async function getWechatLastTts(): Promise<WechatLastTts> {
  return jsonFetch<WechatLastTts>("/wechat/send_voice/last");
}

/** 最近发送的微信语音列表 */
export async function getWechatHistory(): Promise<{ ok: boolean; items: WechatHistoryItem[] }> {
  return jsonFetch<{ ok: boolean; items: WechatHistoryItem[] }>("/wechat/history");
}

/**
 * 全自动发送：切录音设备到 CABLE Output → 前台化微信模拟按住 Alt → 播放 → 松开发送。
 * 执行期间（约 wav 时长 + 3s）不要动键鼠。wav=outputs/ 下文件名，缺省=最近 TTS。
 */
export async function wechatSendVoice(wav?: string): Promise<WechatSendResult> {
  return jsonFetch("/wechat/send_voice", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wav: wav || null }),
  });
}

/** 半自动：播放到虚拟声卡（带静音头），用户自己在微信按住 Alt 录，录完松开发送 */
export async function wechatPlayToCable(wav?: string, leadS?: number): Promise<WechatSendResult> {
  return jsonFetch("/wechat/play_to_cable", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wav: wav || null, lead_s: leadS ?? null }),
  });
}

/** 手动实时变声模式：切微信录音到 CABLE Output + 确保实时变声运行，用户按住 Alt 说话 */
export async function wechatManualSend(): Promise<WechatSendResult> {
  return jsonFetch("/wechat/manual_send", { method: "POST" });
}

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

// ---- 作品库（B1：收藏 / 标签 / 批量导出） ----

export type HistoryKind = "tts" | "offlinevc" | "audiobook" | "fx" | "trial" | "mine";

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

// ---- 存储占用看板（B2） ----

export type StorageDisk = {
  path: string; drive: string; label: string;
  total_bytes: number; used_bytes: number; free_bytes: number; used_percent: number;
};

export type StorageItem = {
  key: string; label: string; desc: string; cleanable: boolean;
  bytes: number; files: number;
};

export type StorageInfo = { disks: StorageDisk[]; items: StorageItem[] };

export async function getStorage(): Promise<StorageInfo> {
  return jsonFetch<StorageInfo>("/system/storage");
}

export type StorageCleanResult = {
  ok: boolean; freed_bytes: number; removed_files: number;
  skipped: { key: string; reason: string }[];
  errors: { file: string; error: string }[];
  cleaned_at: string;
};

export async function cleanStorage(targets: string[]): Promise<StorageCleanResult> {
  return jsonFetch("/system/storage/clean", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets }),
  });
}
