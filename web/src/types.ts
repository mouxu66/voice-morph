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

// 后端预热（/api/system/warmup）：TTS worker / RVC / 微信播放worker 常驻化进度。
// steps 是「已完成」的步（后端边跑边追加），done=true 表示三步全部结束。
export interface WarmupStep {
  step: string;
  seconds: number;
  ok: boolean;
  detail?: string;
}

export interface WarmupStatus {
  running: boolean;
  done: boolean;
  steps: WarmupStep[];
  total_s?: number;
  error?: string;
}

// 发送链路自检（/api/audio/send_chain）；items 复用 DiagnoseItem 结构
export interface SendChainInfo {
  ok: boolean;
  all_ok: boolean;
  stale: boolean;
  items: DiagnoseItem[];
}

// 输字变声链路自检（/api/tts/send_chain）；items 同上，便于复用同一套渲染。
// 故障面与实时变声不同（引擎/参考音/磁盘 vs 声卡），故检查项完全不同。
export interface TtsChainInfo {
  ok: boolean;
  all_ok: boolean;
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
  /** 来源标记：market=市场安装；自训/导入无标记（logs/<id>/source.json） */
  source?: string;
  /** 市场音色的上游许可（G4 回读）：仅在 license_source 存在时出现 */
  source_license?: string;
  /**
   * 许可的来路，三态语义不同、**不能合并展示**（见 m2_server/market_license.py）：
   *   model-card  = 读到了上游许可，source_license 是许可名
   *   unlabeled   = 读通了但上游没标 → 未标注即默认保留所有权利，只能沿用兜底文案
   *   unreachable = 没读通，与"没标"不是一回事，需再查
   */
  license_source?: "model-card" | "unlabeled" | "unreachable" | string;
  license_checked_at?: string;
  /** 市场安装音色已生成的自动试听地址（outputs/market/<id>_preview.wav） */
  preview_url?: string;
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

/** 切片质检结果（P1-1，后端 clip_qc 产出，随 GET /clips 一起返回） */
export interface ClipQc {
  score: number;
  grade: "A" | "B" | "C" | "D";
  reasons: string[];
  duration_s: number;
  spk_sim?: number | null;
}

export interface ClipItem {
  name: string;
  duration_s: number;
  loudness_dbfs: number;
  qc?: ClipQc;
}

export interface PipelineProgress {
  step: string;
  video: string;
  message: string;
  done: boolean;
  clips?: number;
}

export type PipelineStage = "idle" | "extracting" | "separating" | "slicing" | "done" | "error";
