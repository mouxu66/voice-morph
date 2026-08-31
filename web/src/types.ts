// 与 FastAPI 后端(m2_server)交互的数据类型

export interface HealthInfo {
  status: string;
  cuda: boolean;
}

// 环境体检单项（/api/diagnose 返回）；warn=true 表示「不致命的告警」（如退回 CPU）
export interface DiagnoseItem {
  key: string;
  ok: boolean;
  warn?: boolean;
  label: string;
  detail: string;
  hint?: string | null;
}

export interface DiagnoseInfo {
  all_ok: boolean;
  cuda: boolean;
  items: DiagnoseItem[];
}

export interface VoiceInfo {
  id: string;
  display_name?: string;
  reference: string;
  duration_s: number;
  kind?: "clone" | "finetuned" | "rvc_model";
  /** 是否有参考音频（音色库档案有，RVC 纯模型目录无） */
  has_reference?: boolean;
  /** RVC 模型状态字段（kind=rvc_model 或已训练的音色库档案携带） */
  model_ready?: boolean;
  trained_at?: string;
  dataset_count?: number;
  /** 音色入库自动质检结果（tools/voice_qc.py 产出，经 /rvc/voices 的 qc 字段并入） */
  qc?: VoiceQc | null;
}

// 音色质检单项（时长比 / f0偏移 / ASR重合 / 声纹余弦，各 25 分）
export interface VoiceQcItem {
  value: number | null;
  pass: boolean;
  score: number;
  detail: string;
  stage?: string;
  hint?: string;
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
    error_stage?: string | null;
    hint?: string | null;
  } | null;
  dataset?: Record<string, unknown> | null;
  error?: string | null;
  error_stage?: string | null;
  hint?: string | null;
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
