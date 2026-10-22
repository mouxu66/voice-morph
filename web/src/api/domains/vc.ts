import { jsonFetch, BASE } from "../http";

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
