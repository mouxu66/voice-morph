import { jsonFetch, BASE } from "../http";

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

// ---- 快捷短语板（sound.tts）----
//
// 课堂高频用语一键合成/重放：直播、录课时点一下就用当前音色念出来，让嗓子歇一歇。
// 后端把产物持久缓存在 outputs/phrase_cache/（键 = 音色+文字）—— 同一句话第二次
// 点击零等待；这与「合成结果退出即删」的会话产物是两种生命周期，见 voice_phrases.py。

export type VoicePhrase = { id: string; text: string };

export async function voicePhraseList(): Promise<{ ok: boolean; items: VoicePhrase[] }> {
  return jsonFetch("/voice-phrases");
}

export async function voicePhraseCreate(text: string): Promise<{ ok: boolean; item: VoicePhrase }> {
  return jsonFetch("/voice-phrases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function voicePhraseUpdate(id: string, text: string): Promise<{ ok: boolean; item: VoicePhrase }> {
  return jsonFetch(`/voice-phrases/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function voicePhraseDelete(id: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/voice-phrases/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** 点一下：用指定音色（缺省=当前选中）念这条短语；命中缓存时后端直接回磁盘上的旧产物。 */
export async function voicePhraseSpeak(
  id: string,
  voiceId?: string
): Promise<{ ok: boolean; url: string; duration_s: number; cached: boolean; voice_id: string }> {
  return jsonFetch(`/voice-phrases/${encodeURIComponent(id)}/speak`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice_id: voiceId ?? "" }),
  });
}
