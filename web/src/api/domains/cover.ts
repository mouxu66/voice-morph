import { jsonFetch, BASE } from "../http";

// ---- 翻唱（扒歌换声：整首歌 → 分离 → 换音色 → 合回伴奏） ----

export type CoverStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "error";
  /** 当前工序，仅 running 时有意义。前端用它显示"到哪一步了" */
  step: "" | "separate" | "convert" | "mix";
  message: string;
  percent: number;
  voice_id: string;
  url: string;
  duration_s: number;
  /** 实际使用的变调（auto_pitch 时与提交值不同，以此为准） */
  pitch: number;
  /**
   * 实际用到的人声增益（0 = 本轮还没算出来）。auto_gain 时它是后端按实测 RMS
   * 算出来的数，与提交的 vocal_gain 无关 —— 完成面板必须如实显示它，否则
   * "滑块拖了半天没动、音量却变了"这件事用户永远无法从界面得知（2026-09-27 修）。
   */
  vocal_gain_applied: number;
  error: string;
};

/** 粘直链下载的结果（`/cover/fetch`）：name 是会话里的裸名，url 可直接试听 */
export type CoverFetchResult = {
  ok: boolean;
  name: string;
  url: string;
  bytes: number;
  duration_s: number;
};

/**
 * 把一条音频直链下到会话目录（**只下载，不跑链路**）。
 *
 * 分成两步是为了"先试听再跑"：翻唱要几分钟，而下错歌（版本不对 / 纯伴奏）是最常见的失误。
 * 下好的产物就在会话目录里，退出即删；开跑时用 `src_name` 交给 `/cover/run`。
 */
export async function fetchCoverUrl(url: string): Promise<CoverFetchResult> {
  return jsonFetch<CoverFetchResult>("/cover/fetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
}

export async function runCover(
  file: File | null,
  voiceId: string,
  pitch: number,
  indexRate: number,
  vocalGain = 1,
  accompGain = 1,
  /** true = 忽略传入 pitch，由后端按音域差自动算建议变调 */
  autoPitch = false,
  /** 会话里已下好的源文件名（粘直链那条路）；与 file 二选一 */
  srcName = "",
  /**
   * true（默认）= 后端按实测 RMS 自动配平人声，**覆盖**上面的 vocal/accompGain；
   * false = 以上方两个滑块为准。必须显式发送 —— 后端默认 True，不传这个字段
   * 时两个增益滑杆就是死控件（2026-09-27 修）。
   */
  autoGain = true,
  /**
   * true = 先剥离和声/伴唱、只换主唱（有人声叠唱的歌更稳；依赖独立分离环境
   * `.venv_sep`，缺环境时后端 422）。与 auto_gain 同理**显式发送**：把"勾选态
   * 与请求字段"在结构上绑死，将来后端改默认也不会两边悄悄漂开（2026-10-15）。
   */
  leadVocal = false,
): Promise<{ ok: boolean; voice_id: string }> {
  const form = new FormData();
  if (file) form.append("file", file);
  if (srcName) form.append("src_name", srcName);
  form.append("voice_id", voiceId);
  form.append("pitch", String(pitch));
  form.append("index_rate", String(indexRate));
  form.append("vocal_gain", String(vocalGain));
  form.append("accomp_gain", String(accompGain));
  form.append("auto_pitch", String(autoPitch));
  form.append("auto_gain", String(autoGain));
  form.append("lead_vocal", String(leadVocal));
  const res = await fetch(BASE + "/cover/run", { method: "POST", body: form });
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

export async function getCoverStatus(): Promise<CoverStatus> {
  return jsonFetch("/cover/status");
}

/**
 * 主唱剥离能力状态（GET /cover/lead-status）。
 *
 * 这个功能依赖**独立环境 + 独立模型**（`.venv_sep` + karaoke ckpt，见
 * `m2_server/lead_sep.py`）。界面的口径：**先问"能不能用、缺什么"，再把勾选框
 * 置灰并说明原因** —— 不让用户勾上之后才在开工前撞 422（后端也会拦，这里是
 * 同一信息的提前量）。
 */
export type LeadStatus = {
  available: boolean;
  model: string;
  venv_python: string;
  model_exists: boolean;
  /** 缺什么（人话列表），空数组 = 就绪 */
  missing: string[];
};

export async function getCoverLeadStatus(): Promise<LeadStatus> {
  return jsonFetch("/cover/lead-status");
}

/** 只算变调建议（不跑完整链路）：分离人声 → 比中位基频 → 返回半音数 */
export async function suggestCoverPitch(
  file: File | null,
  voiceId: string,
  /** 会话里已下好的源文件名（粘直链那条路）；与 file 二选一 */
  srcName = ""
): Promise<{
  ok: boolean;
  pitch: number;
  /** 分析出的原始建议（未吸附八度）；旧后端不返回时视为与 pitch 相同 */
  raw_pitch?: number;
  /** true = 原始建议不是整八度、已被吸附（伴奏不随人声变调，非八度会错调） */
  octave_snapped?: boolean;
}> {
  const form = new FormData();
  if (file) form.append("file", file);
  if (srcName) form.append("src_name", srcName);
  form.append("voice_id", voiceId);
  const res = await fetch(BASE + "/cover/pitch_suggest", { method: "POST", body: form });
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
