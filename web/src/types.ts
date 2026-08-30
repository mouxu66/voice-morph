// 与 FastAPI 后端(m2_server)交互的数据类型

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
  /** 音色入库自动质检结果（tools/voice_qc.py 产出，经 /rvc/voices 的 qc 字段并入） */
  qc?: VoiceQc | null;
}

// 音色质检单项（时长比 / f0偏移 / ASR重合 / 声纹余弦，各 25 分）
export interface VoiceQcItem {
  value: number | null;
  pass: boolean;
  score: number;
  detail: string;
}

// 音色质检结果：outputs/qc/<exp>.json 的结构（dataset/voice 两节可并存）
export interface VoiceQc {
  exp: string;
  created_at?: string;
  score?: number | null;
  pass?: boolean;
  voice?: {
    items?: Record<string, VoiceQcItem>;
    score?: number | null;
    pass?: boolean;
    input?: string;
    output?: string;
    emb_ref?: string;
    self_convert?: boolean;
    error?: string | null;
  } | null;
  dataset?: Record<string, unknown> | null;
  error?: string | null;
}

export interface VoiceList {
  voices: VoiceInfo[];
}

export interface VideoItem {
  name: string;
  size_mb: number;
  used_by?: string[];
}

export interface ClipItem {
  name: string;
  duration_s: number;
  loudness_dbfs: number;
}

export interface PipelineProgress {
  step: string;
  video: string;
  message: string;
  done: boolean;
  clips?: number;
}

export type PipelineStage = "idle" | "extracting" | "separating" | "slicing" | "done" | "error";
