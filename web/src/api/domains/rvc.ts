import type { VoiceQc } from "../../types";
import { jsonFetch } from "../http";

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
  /** RVC 实验名（= 音色 id）。后端未指定默认音色时为空字符串。 */
  exp: string;
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
