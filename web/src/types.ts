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
}

export interface VoiceList {
  voices: VoiceInfo[];
}

export interface VideoItem {
  name: string;
  size_mb: number;
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
