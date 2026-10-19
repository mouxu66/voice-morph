import type {
  CapabilityInfo,
  ClipItem,
  DiagnoseInfo,
  HealthInfo,
  LanInfo,
  PluginCatalog,
  PluginPresetResult,
  PluginToggleResult,
  SendChainInfo,
  TtsChainInfo,
  VideoItem,
  VoiceInfo,
  VoiceQc,
  WarmupStatus,
} from "../types";

// 预热状态的结构定义在 types.ts，这里转出供页面直接 import（与本地定义的 Wechat* 类型并列）
export type { WarmupStatus };
export type { LanInfo };

// 后端统一挂在 /api 前缀下。全仓**只在这里**判定一次"该用相对还是绝对地址"
// （2026-09-13 收敛：此前 client.ts / useEffects.ts / useWorkshop.ts 各写了一份）。
//
// Electron(file://) 页面直连的后端地址。页面由 http(s) 提供时（dev 的 Vite 5173、
// 后端自托管的局域网页面）一律走同源相对路径，用不到它。
export const BACKEND_ORIGIN = "http://127.0.0.1:8000";

/**
 * 后端地址前缀：
 *   - dev(Vite 5173) / 页面由 http(s) 提供 → ""（同源相对路径；dev 由 vite proxy 转发）
 *   - Electron 打包态 file:// 页面 → BACKEND_ORIGIN（跨源直连本机 8000）
 *
 * 注意后者不能写成 `import.meta.env.DEV ? ... : BACKEND_ORIGIN`：局域网用户
 * （手机浏览器打开 http://<PC-IP>:8000）跑的是**构建产物**，DEV 为 false，
 * 那样会拼出 127.0.0.1 —— 在手机上指向手机自己，必然失败。判定依据必须是
 * "页面协议"，不是构建模式。
 */
export function backendPrefix(): string {
  if (import.meta.env.DEV) return "";
  if (typeof location !== "undefined" && (location.protocol === "http:" || location.protocol === "https:")) {
    return "";
  }
  return BACKEND_ORIGIN;
}

export const BASE = backendPrefix() + "/api";

// 把后端返回的相对音频路径（如 /media/outputs/x.wav）转成可播放的绝对地址。
// 生产环境后端返回的 url 是 /api/media/...，这里做兜底拼接。
export function mediaUrl(path: string): string {
  if (!path) return "";
  if (/^https?:\/\//.test(path)) return path;
  if (path.startsWith("/api/")) return backendPrefix() + path;
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

// ---- 会话产物（「即用即删」）----
// 合成结果默认落在 outputs/.session/，退出即清空；只有调 saveSession 才会复制进
// 作品库。删（purge）在退出时由 Electron 主进程与后端启动各自兜底，前端只在用户
// 主动「清空本次」时调。见后端 session_out.py。

export type SessionSaveResult = {
  ok: boolean;
  name: string;
  url: string;
  /** 历史记录 id（`<毫秒>_<hex6>` 形式的字符串，见 history.register） */
  item_id: string;
  already: boolean;
};

/**
 * 把一条会话产物保存进作品库（复制到 outputs 根 + 登记历史）。幂等：
 * 重复保存同一个 wav 只会得到第一次那条记录，不会产生第二份文件。
 */
export async function saveSession(
  wav: string,
  opts: { kind?: string; voiceId?: string; inputText?: string } = {}
): Promise<SessionSaveResult> {
  return jsonFetch<SessionSaveResult>("/session/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      wav,
      kind: opts.kind ?? "tts",
      voice_id: opts.voiceId ?? "",
      input_text: opts.inputText ?? "",
    }),
  });
}

/** 清空会话目录，返回删掉的文件数。 */
export async function purgeSession(): Promise<{ ok: boolean; removed: number }> {
  return jsonFetch<{ ok: boolean; removed: number }>("/session/purge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

export async function getHealth(): Promise<HealthInfo> {
  return jsonFetch<HealthInfo>("/health");
}
/**
 * 后端能力加载清单：哪些路由模块真的挂上了、哪些没挂上及原因。
 *
 * 单开一个端点而不复用 `/health`：后者第一行就是 `import torch`，没 torch 时它自己
 * 就 500 —— 而「某个能力缺依赖」恰恰是那种环境里最需要看到的信息，
 * 挂在 /health 上等于在最需要的时候消失。
 */
export async function getCapabilities(): Promise<CapabilityInfo> {
  return jsonFetch<CapabilityInfo>("/capabilities");
}

/**
 * 能力清单（插件目录）：每个能力的声明 + 三态状态 + 依赖关系。
 *
 * 与 `getCapabilities` 的分工：那边答「26 个路由**模块**逐个挂上没有」，是加载器的原始账本；
 * 这里答「**能力**视角」—— 哪些可用、哪些被用户关掉、每个要装什么、缺了会怎样。
 *
 * **只读**：开关能力是插件化第 6 步（`POST /api/plugins/{id}/enable|disable`）。
 * 后端实现同样不依赖重库（没有模块级 `import torch`），所以重依赖崩掉时它仍能答 ——
 * 这正是最需要这份清单的时刻。
 */
export async function getPlugins(): Promise<PluginCatalog> {
  return jsonFetch<PluginCatalog>("/plugins");
}

/**
 * 开关一个能力（插件化第 6 步）。
 *
 * 失败时 `jsonFetch` 会把后端的 `detail` 原样抛出来 —— **别过 `friendlyError()`**：
 * 它把 409 一律翻译成「同名文件已存在」，而这里的 409 说的是
 * 「你还被某某依赖着，先关掉它」，语义完全不同。
 *
 * **重启生效**：router 在后端启动时挂好，本轮不做热插拔，所以返回里带
 * `restartRequired`，界面必须如实转述（否则用户点了开关看不到变化，以为坏了）。
 */
export async function setPluginEnabled(id: string, enabled: boolean): Promise<PluginToggleResult> {
  const verb = enabled ? "enable" : "disable"
  return jsonFetch<PluginToggleResult>(`/plugins/${encodeURIComponent(id)}/${verb}`, { method: "POST" })
}

/**
 * 套用一个套餐预设（§8.1）。同样是重启生效。
 *
 * ★ 默认**只增不减**（不传 `strict`）：只补齐该套餐点名的能力，绝不关闭用户手动开启的项。
 * 要「收窄到只剩这个套餐」必须显式 `{ strict: true }` —— 界面上它是一个单独的
 * 「严格套用（会关掉 N 项）」动作 + 确认，**不是点一下模式卡片就跑的副作用**
 * （2026-10-15 用户拍板，理由见 `lib/modeSwitch.ts` 头注释）。
 */
export async function applyPluginPreset(
  preset: string,
  opts: { strict?: boolean } = {},
): Promise<PluginPresetResult> {
  return jsonFetch<PluginPresetResult>("/plugins/preset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preset, strict: opts.strict ?? false }),
  })
}

/** 环境体检：检查 ffmpeg / RVC 整合包 / 默认音色权重 / CUDA / TTS 模型 等本机依赖 */
export async function diagnose(): Promise<DiagnoseInfo> {
  return jsonFetch<DiagnoseInfo>("/diagnose");
}

/**
 * AI 调试接口的只读诊断快照（Markdown）：环境体检 + 能力加载 + 音频设备 +
 * 预热 + 微信发送预检 + 最近产出（不含文字内容）+ 疑似错误日志，后端已脱敏。
 *
 * 为什么走它而不是前端自己把 `/diagnose` 拼一段文字：这份报告要能回答
 * 「这次为什么失败」（哪个能力没挂、音频链路缺什么、上一条发送有什么 warning），
 * 而 `/diagnose` 只答「装没装」。设计见 `docs/AI调试端点设计.md`。
 * 端点仅限本机访问；后端不可用时抛错，调用方自行降级（见 EnvHealth.copyReport）。
 */
export async function getDiagnosticReport(): Promise<string> {
  const res = await fetch(`${BASE}/ai/context?format=md`, {
    headers: { Accept: "text/markdown" },
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
  return res.text();
}

/** `/api/ai/actions` 的动作目录返回体（只读面，任何时候都能读）。 */
export interface AiActionsCatalog {
  schema: number;
  write_enabled: boolean;
  write_reason: string;
  /** 缺了哪几道门 —— 逐条点名，别让用户猜 */
  missing_gates: string[];
  actions: Array<{
    id: string;
    desc: string;
    risk: string;
    schema: Record<string, string>;
  }>;
  denied: Array<{ id: string; reason: string }>;
}

/**
 * 动作目录：能做什么（schema + 风险）、不能做什么（拒绝清单）、以及写能力现在开着没。
 *
 * `write_enabled` 是**实时**判据（三重门），不是「有没有写插件」——
 * 用户设了 token 但忘了 `VM_AI_CONTROL=1` 时它也是 false，而 `missing_gates`
 * 会点名缺哪一项，不让调用方去猜。
 */
export async function getAiActions(): Promise<AiActionsCatalog> {
  return jsonFetch<AiActionsCatalog>("/ai/actions");
}

/** 审计账本里的一条记录。 */
export interface AiAuditEntry {
  id: string;
  ts: number;
  action: string;
  params: Record<string, unknown>;
  dry_run: boolean;
  ok: boolean;
  code: number;
  duration_ms: number;
  actor: string;
  evidence: unknown;
  message: string;
}

export interface AiAuditTail {
  schema: number;
  items: AiAuditEntry[];
  total: number;
  source: string;
  note: string;
}

/**
 * AI 操作记录（只读，倒序）。
 *
 * 刻意与 `hook.ai` 的启用状态解耦：用户关掉写能力后**仍然该看得见**
 * 「关掉之前 AI 做过什么」—— 关插件是为了止血，不是为了销毁证据。
 */
export async function getAiAudit(limit = 100): Promise<AiAuditTail> {
  return jsonFetch<AiAuditTail>(`/ai/audit?limit=${limit}`);
}

/**
 * 后端预热进度（TTS worker / RVC / 微信播放worker 常驻化，合计约 50s）。
 * 预热完成前发语音会卡在模型加载上（请求会等预热结束，不会失败），
 * 前端拿它把「为什么第一次这么慢」讲清楚。
 */
export async function getWarmup(): Promise<WarmupStatus> {
  return jsonFetch<WarmupStatus>("/system/warmup");
}

/**
 * 本机局域网访问信息：遥控页二维码的数据源。
 *
 * 手机浏览器做不了 mDNS 服务发现（网页没有 Bonjour 能力，`.local` 在安卓上
 * 也不可靠），所以「扫码即开」是浏览器场景下自动化的天花板 —— issue #1 的对症方案。
 * URL 里已按需拼好 `?api_key=`（后端负责），前端拿来即用，不要自己再拼。
 */
export async function getLan(): Promise<LanInfo> {
  return jsonFetch<LanInfo>("/system/lan");
}

export async function listVoices(): Promise<VoiceInfo[]> {
  const data = await jsonFetch<{ voices: VoiceInfo[] }>("/voices");
  return data.voices;
}

/**
 * 音色清单 + **主界面当前选中的音色**（`/voices` 响应里的 `selected`）。
 *
 * ★ 为什么单独开一个函数而不改 `listVoices`：清单本身有 3 个消费方
 * （素材流水线 / 级联 / 种子音色）只要数组，改成返回对象会连带改它们，
 * 收益为零。而 `selected` 只有启动同步那一处要。
 *
 * ★ 为什么必须带上它：`selectedVoiceId` 是**后端**的状态（写在
 * `media/voicebank/selected_voice.json`，音色挖掘/声音保存时落盘），前端 store
 * 的初值是 `null` 且**只在用户手动下拉选择时**才被写入 —— 不在这里同步回来，
 * 那么「后端明明有选中音色，前端却认为一个都没选」，表现是：
 *   1. 快捷短语板底部那句橙色提示「先在上方选择音色…」**常亮**（`hasVoice` 恒 false）；
 *   2. 录课页「开始合成」按钮永久变灰。
 *
 * ⚠️ 但它**不会**让短语板播不出声 —— `usePhraseBoard.play()` 的守卫只看 `backendUp`，
 * 照样发请求，后端 `voice_id = body.voice_id or _selected_voice()` 会兜住。
 * （2026-10-04 我一度把「Alt+1 无声」错判成这条的后果，实际根因是菜单栏抢 Alt。）
 *
 * `selected` 为空串 = 后端也没选（或选中的音色已被删除，后端会按未选中处理）。
 */
export async function listVoicesWithSelected(): Promise<{
  voices: VoiceInfo[];
  selected: string;
}> {
  const data = await jsonFetch<{ voices: VoiceInfo[]; selected?: string }>("/voices");
  return { voices: data.voices ?? [], selected: data.selected ?? "" };
}

// ---- M1 素材流水线（后端需提供以下端点） ----

export async function listRawVideos(): Promise<VideoItem[]> {
  const data = await jsonFetch<{ videos: VideoItem[] }>("/raw_videos");
  return data.videos;
}

export async function deleteRawVideo(name: string, force: boolean): Promise<{ ok: boolean; clips: number; related: number }> {
  return jsonFetch(`/raw_videos/${encodeURIComponent(name)}?force=${force}`, { method: "DELETE" });
}

/** P2-1 失败可诊断结论：由后端 `quality_verdict.build_quality_verdict()` 产出。
 *  `verdict` 是机器判定码，配色/文案查 `lib/qualityVerdict.ts` 的 VERDICT_META ——
 *  不要在这里再写一份 label（两份 label 必然对不上）。 */
export type PipelineVerdict = {
  verdict: string;
  title: string;
  detail: string;
  actions: string[];
  ok_ratio: number;
  total: number;
  ok: number;
  grades: { A: number; B: number; C: number; D: number };
  reason: string;
};

export type PipelineStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  step: string;
  message: string;
  percent: number;
  clips: number;
  error: string;
  /** 可能缺省（旧后端 / 未跑过质检）—— 前端一律当作「没有结论」处理 */
  verdict?: PipelineVerdict | null;
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

// ---- 讲稿录课（PPT 备注页 → 逐页旁白，sound.course） ----

export type CoursePageIn = {
  page: number;
  text: string;
  /**
   * 这页讲稿从哪来：`remark`（PPT 备注页）/ `body`（备注为空时自动取该页幻灯片正文）。
   * 贴文本的批次没有这个字段。前端据此标一句「这页用的是正文草稿」，老师可回改。
   */
  source?: "remark" | "body";
};

export type CoursePageState = {
  page: number;
  text: string;
  url: string;
  duration_s: number;
  failed: boolean;
  error: string;
};

export type CourseStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  voice_id: string;
  /**
   * 风格参考 ICL 的音色 id（空=不启用，走纯声纹克隆）。
   * ⚠️ 非空时是**整段克隆**：声音与语气都取自参考音，`voice_id` 那份音色不参与发声。
   */
  style_ref_voice: string;
  done: number;
  total: number;
  percent: number;
  current_page: number;
  current_text: string;
  job_dir: string;
  gap_ms: number;
  pages: CoursePageState[];
  error: string;
};

export type CourseParseResult = {
  source: "pptx" | "text";
  total: number;
  skipped: number;
  pages: CoursePageIn[];
  /**
   * 上传的 .pptx 在会话目录里的暂存裸名（贴文本时为空串）。
   *
   * 合成时要原样回传：成片的画面就是从这份**原课件**导出来的
   * （后端 parse → 暂存 → run 复制进产物目录 → 导出时读它）。
   */
  source_pptx: string;
};

/** 解析讲稿来源：.pptx 附件优先，否则按纯文本（`---` 分页）。二选一。 */
export async function parseCourse(
  file: File | undefined,
  text: string
): Promise<CourseParseResult> {
  const form = new FormData();
  if (file) form.append("file", file);
  form.append("text", text);
  const res = await fetch(BASE + "/course/parse", { method: "POST", body: form });
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

export async function runCourse(
  pages: CoursePageIn[],
  voiceId: string,
  gapMs = 350,
  styleRefVoice = "",
  sourcePptx = ""
): Promise<{ ok: boolean; total: number; voice_id: string; job_dir: string }> {
  return jsonFetch("/course/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pages,
      voice_id: voiceId,
      gap_ms: gapMs,
      style_ref_voice: styleRefVoice,
      source_pptx: sourcePptx,
    }),
  });
}

export async function getCourseStatus(): Promise<CourseStatus> {
  return jsonFetch("/course/status");
}

export async function cancelCourse(): Promise<{ ok: boolean; message?: string }> {
  return jsonFetch("/course/cancel", { method: "POST" });
}

export async function retryCourse(): Promise<{
  ok: boolean;
  total: number;
  voice_id: string;
  job_dir: string;
}> {
  return jsonFetch("/course/retry", { method: "POST" });
}

/**
 * 导出成片（课件画面 + 逐页旁白 → mp4）。
 *
 * ⚠️ 画面取自学**原课件**（合成时复制进产物目录的那份），所以贴文本的批次会 400
 * —— 那句 detail 直接给用户看（"上传 .pptx 重新合成一次，成片才有画面"）。
 */
export type CourseVideoStatus = {
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  step: string; // slides | mux | ""
  percent: number;
  message: string;
  job_dir: string;
  out_url: string;
  out_name: string;
  duration_s: number;
  pages: number; // 成片用了多少页画面
  slides: number; // 课件一共导了多少页
  fps: number;
  gap_ms: number;
  error: string;
};

export type CourseVideoOptions = {
  fps?: number;
  gapMs?: number;
  forceSlides?: boolean;
  /** 指定批次（留空 = 当前这批）；pin 住它是为了"导出的一定是眼前这一批" */
  jobDir?: string;
};

export async function runCourseVideo(
  opts: CourseVideoOptions = {}
): Promise<{ ok: boolean; job_dir: string; pages: number; fps: number; gap_ms: number }> {
  return jsonFetch("/course/video", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      fps: opts.fps ?? 30,
      gap_ms: opts.gapMs, // undefined 会被 JSON.stringify 丢掉 → 后端用这一批的 gap
      force_slides: opts.forceSlides ?? false,
      job_dir: opts.jobDir ?? "",
    }),
  });
}

export async function getCourseVideoStatus(): Promise<CourseVideoStatus> {
  return jsonFetch("/course/video/status");
}

export async function cancelCourseVideo(): Promise<{ ok: boolean; message?: string }> {
  return jsonFetch("/course/video/cancel", { method: "POST" });
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

/** 输字变声链路自检：只读检查引擎模型/引擎进程/当前音色参考音/输出目录。 */
export async function ttsChainCheck(): Promise<TtsChainInfo> {
  return jsonFetch<TtsChainInfo>("/tts/send_chain");
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
  /** 性能档位：balanced=均衡·音质优先 / game=游戏低占用 */
  perf_profile?: string;
  perf_profile_desc?: string;
  /** 推理子进程的线程上限；0 = 不注入（交系统默认，会按逻辑核数开池） */
  omp_threads?: number;
  /** GPU 显存（MB）；无 GPU 或 nvidia-smi 不可用时为 null */
  gpu_total_mb?: number | null;
  gpu_used_mb?: number | null;
  /** 实时变声子进程显存（MB）；探测不到时为 null */
  live_proc_vram_mb?: number | null;
  /** 语音合成引擎（Qwen3-TTS worker）是否驻留显存：game 档卸载后为 false */
  tts_worker_alive?: boolean;
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

export async function rvcLiveStart(
  expName?: string,
  opts?: { monitor?: boolean; monitorGain?: number },
): Promise<RvcStartResult> {
  const q = new URLSearchParams()
  if (expName) q.set("exp_name", expName)
  // 试音间要 "自己说话就听到变声"，必须显式开自我监听（game 档默认关）
  if (opts?.monitor !== undefined) q.set("monitor", String(opts.monitor))
  if (opts?.monitorGain !== undefined) q.set("monitor_gain", String(opts.monitorGain))
  const qs = q.toString()
  return jsonFetch<RvcStartResult>(`/rvc/live/start${qs ? `?${qs}` : ""}`, { method: "POST" })
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

export type LiveProfileResult = {
  ok: boolean;
  profile: string;
  profile_desc?: string;
  restarted?: boolean;
  gpu_total_mb?: number | null;
  gpu_used_mb?: number | null;
  live_proc_vram_mb?: number | null;
  /** 语音合成引擎是否驻留显存：game 档卸载后为 false */
  tts_worker_alive?: boolean;
  /** 本次切换释放的显存（MB）；未切到 game 或 worker 本就未驻留时为 0 */
  tts_freed_mb?: number;
  /** 合成中切 game：worker 未立即卸载，等任务结束后自动释放（此时 tts_freed_mb 为 0） */
  tts_freed_deferred?: boolean;
};

export async function rvcLiveGetProfile(): Promise<LiveProfileResult> {
  return jsonFetch("/rvc/live/profile");
}

export async function rvcLiveSetProfile(profile: string): Promise<LiveProfileResult> {
  return jsonFetch("/rvc/live/profile", { method: "POST", body: JSON.stringify({ profile }) });
}

// ---- 场景包（2026-09-29）----
// 场景 = 给「我现在要开会 / 开黑」起个名字，一键把整组开关写到位。
// 两类字段语义不同，别混（后端 live_settings.SCENES 的注释是权威）：
//   in_settings —— 落盘，持久生效
//   on_start    —— 不落盘，只在应用那一刻随变声启动传下去

export type LiveScene = {
  key: string;
  label: string;
  desc: string;
  icon?: string;
  order: number;
  /** 会落盘的设置（持久生效） */
  in_settings: Record<string, string | boolean>;
  /** 只在应用时随启动传下去的伴随项（自我监听 / 字幕），不持久 */
  on_start: Record<string, boolean>;
};

export type LiveScenesResult = {
  ok: boolean;
  scenes: LiveScene[];
  /** 当前高亮的场景；用户手动改过单开关后为 null */
  active: string | null;
  /** 上次点过的场景（可能与 active 不同 = 之后被手动改过） */
  stored: string;
  current: Record<string, unknown>;
};

export type LiveSceneApplyResult = {
  ok: boolean;
  scene: string;
  label: string;
  applied: Record<string, string | boolean>;
  /** 已随本次重启生效的伴随项；没重启时为空对象 */
  applied_on_start: Record<string, boolean>;
  /** 运行中但未重启：这些伴随项还没生效 */
  on_start_pending: Record<string, boolean>;
  needs_restart: boolean;
  restarted: boolean;
  tts_freed_mb?: number;
  settings: Record<string, unknown>;
};

export async function rvcLiveGetScenes(): Promise<LiveScenesResult> {
  return jsonFetch("/rvc/live/scenes");
}

export async function rvcLiveApplyScene(
  scene: string,
  restart = true,
): Promise<LiveSceneApplyResult> {
  return jsonFetch("/rvc/live/scene", {
    method: "POST",
    body: JSON.stringify({ scene, restart }),
  });
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
  /**
   * 这条现在还能不能重发/试听。
   *
   * 产物默认是**会话级**的（退出即删，见 `session_out` 模块注释里用户的原话），
   * 所以历史里多数条目在退出应用后文件已不在磁盘上。后端每次请求现算这个字段
   * （可用性是动态的）。**缺省按「不可用」处理**：宁可少给一个按钮，
   * 也不要摆一个按下去必然报错的「重发」。
   */
  available?: boolean;
  /** 长文分段发送：这一批总共几条（单条不发这个字段）。 */
  total_chunks?: number;
  /** 发送时的告警（如静音检测），落库供事后查线索。 */
  warning?: string;
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

// ---- 特效声板（把短音效实时播进虚拟声卡） ----
// 声板归属 `sound.fx-board` 插件；调用点必须门控（见 pages/Tts/SoundboardPanel.tsx）。

export type SoundboardItem = {
  id: string
  name: string
  tags: string[]
  /** 格子图标（emoji），来自 manifest；后端老一点时可能是空串 → 渲染层退到通用图标。 */
  icon: string
  duration_s: number
  count: number
  builtin: boolean
  /** 来源音效包 id；空串 = 出厂或单条导入。包内素材的 id 形如 `"<pack>/<stem>"`。 */
  pack: string
  /** 能不能**单条**删。包内素材与出厂素材都是 false（前者要整包卸）。 */
  removable: boolean
}

/** 已安装/可安装的音效包（成套素材，一次装卸一批）。 */
export type SoundboardPack = {
  id: string
  name: string
  author?: string
  license?: string
  description?: string
  count: number
  bytes?: number
  /** 非空 = 这个包坏了（缺/损 pack.json），界面要如实说出来而不是当它不存在。 */
  broken?: string
  /** 货架条目：这个包现在装没装。 */
  installed?: boolean
  downloads?: number
  url?: string
  sha256?: string
  size?: number
}

export async function soundboardCatalog(): Promise<{ ok: boolean; items: SoundboardItem[] }> {
  return jsonFetch<{ ok: boolean; items: SoundboardItem[] }>("/soundboard/catalog")
}

/** 播放一条音效到虚拟声卡。**立即返回**（one-shot 语义：点击即响，不等播完）。 */
export async function soundboardPlay(
  id: string,
  gain?: number,
): Promise<{ ok: boolean; playing: boolean; id: string; duration_s?: number | null }> {
  return jsonFetch("/soundboard/play", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, gain: gain ?? null }),
  })
}

export async function soundboardStop(): Promise<{ ok: boolean }> {
  return jsonFetch("/soundboard/stop", { method: "POST" })
}

/** 预热常驻播放器（冷导入 ~2-3s 只付一次，否则第一次点击听着像"没反应"）。 */
export async function soundboardWarm(): Promise<{ ok: boolean; ready: boolean; samples: number }> {
  return jsonFetch("/soundboard/warm", { method: "POST" })
}

/** 混淆位置：叠加（与人声同时）/ 拼在开头 / 拼在结尾。与后端 `sfx_lib.MODES` 同义。 */
export type SoundboardMode = "layer" | "prepend" | "append"

export type SoundboardInsert = {
  sample: string
  mode: SoundboardMode
  at_s?: number
  gain?: number
}

/**
 * 预混：把勾选的音效**离线混进**一条合成产物，产出新 wav（`sfxmix_<原名>`）。
 *
 * 与 `soundboardPlay`（实时播进声卡）的分工：实时是主场景，**预混是确定性兜底** ——
 * 点格子会把鼠标焦点从微信挪走，"按住 Alt 的录音会不会被取消"还没真机验过；
 * 预混不依赖任何交互，把发送目标换成返回的 `wav` 即可。
 *
 * 端点属 `sound.fx-board`（不是 `sound.effects`）：关掉效果器时预混仍可用。
 * 失败是**硬失败**（差一条音效就 4xx，`jsonFetch` 把 detail 抛出来）——
 * 静默跳过会变成"发出去的语音里少了那一声，而界面一切正常"，那是最难发现的失败。
 */
export async function soundboardPremix(
  wav: string | undefined,
  inserts: SoundboardInsert[],
): Promise<{ ok: boolean; wav: string; seconds: number; inserts: number; skipped: string[] }> {
  return jsonFetch("/soundboard/premix", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wav: wav ?? null, inserts }),
  })
}

// ------------------------------------------------ BGM 垫底（歌声垫底三档，端点属 sound.fx-board）

/** 上传一首歌当 BGM 素材：服务端转成单声道 PCM16 wav 落会话目录，返回裸名。 */
export async function bgmUpload(
  file: File,
): Promise<{ ok: boolean; name: string; duration_s: number; url: string }> {
  const fd = new FormData()
  fd.append("file", file)
  return jsonFetch("/soundboard/bgm/upload", { method: "POST", body: fd })
}

/**
 * A 档「歌声垫底预混」：把一首歌全程垫进一条合成产物，产出 `bgmmix_<原名>.wav`。
 * 人声是时间轴（输出长度=人声长度）；`lead_db` = BGM 比人声低多少 dB（配平目标）；
 * `start_s` 用来跳前奏。notes 是提示（循环补满/电平量不出），不是失败。
 */
export async function bgmPremix(params: {
  wav?: string
  bgm: string
  lead_db?: number
  fade_in_s?: number
  fade_out_s?: number
  start_s?: number
}): Promise<{ ok: boolean; wav: string; seconds: number; url: string; notes: string[] }> {
  return jsonFetch("/soundboard/bgm/premix", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  })
}

/** C 档「实时垫歌」：往 CABLE 垫一层 BGM，与说话/变声/one-shot 音效并行不抢占。 */
export async function bgmStart(
  bgm: string,
  gain?: number,
  loop = true,
): Promise<{ ok: boolean; playing: boolean; id: string; duration_s: number | null; loop: boolean }> {
  return jsonFetch("/soundboard/bgm/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bgm, gain: gain ?? null, loop }),
  })
}

/** 停掉垫底层（不影响 one-shot 音效 —— 两条独立的流）。 */
export async function bgmStop(): Promise<{ ok: boolean }> {
  return jsonFetch("/soundboard/bgm/stop", { method: "POST" })
}

/** 实时调垫底层音量（流里读的是同一个变量，不用重开）。 */
export async function bgmGain(gain: number): Promise<{ ok: boolean; gain: number }> {
  return jsonFetch("/soundboard/bgm/gain", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ gain }),
  })
}

/**
 * B 档「翻唱直发」：把一条会话产物截成 ≤54s 的一段（微信语音 60s 硬上限，
 * 音频预算 54s，见 `docs/微信语音-长文分段发送方案.md` §2）。端点属翻唱插件
 * （cover 路由），产物 `clip_<原名>.wav`，`seconds` 是实际截到的时长。
 */
export async function coverClip(
  srcName: string,
  startS = 0,
  seconds = 54,
): Promise<{ ok: boolean; wav: string; seconds: number; total_s: number; url: string; notes: string[] }> {
  return jsonFetch("/cover/clip", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ src_name: srcName, start_s: startS, seconds }),
  })
}

/** 装一个本地 zip 音效包（离线路径：别人发你的包、或你自己打的包）。 */
export async function soundboardPackInstall(
  file: File,
  packId?: string,
  overwrite = false,
): Promise<{ ok: boolean; id: string; count: number; name: string; license: string }> {
  const fd = new FormData()
  fd.append("file", file)
  if (packId) fd.append("pack_id", packId)
  if (overwrite) fd.append("overwrite", "true")
  // 刻意不走 jsonFetch 的 JSON 头：FormData 要浏览器自己带 boundary。
  return jsonFetch("/soundboard/packs/install", { method: "POST", body: fd })
}

/** 已安装的音效包 + 它们的素材总数。 */
export async function soundboardPacks(): Promise<{
  ok: boolean
  packs: SoundboardPack[]
  samples: number
}> {
  return jsonFetch("/soundboard/packs")
}

/** 货架：清单里可下载的包。未配清单时 `items` 为空 + 一句 `note`（不是错误）。 */
export async function soundboardPacksAvailable(): Promise<{
  ok: boolean
  source: string | null
  items: SoundboardPack[]
  error: string
  note: string
}> {
  return jsonFetch("/soundboard/packs/available")
}

/** 从清单地址下载并安装（带 sha256 校验与域名白名单）。 */
export async function soundboardPackDownload(
  id: string,
  overwrite = false,
): Promise<{ ok: boolean; id: string; count: number; sha256_verified: boolean }> {
  return jsonFetch("/soundboard/packs/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, overwrite }),
  })
}

/** 卸载一个音效包：它的素材与使用计数一起消失。 */
export async function soundboardPackUninstall(
  id: string,
): Promise<{ ok: boolean; id: string; count: number; purged: string[] }> {
  return jsonFetch(`/soundboard/packs/${encodeURIComponent(id)}`, { method: "DELETE" })
}

/** 导入一条自己的素材（≤5s、≤2MB，服务端转单声道 PCM16 落盘）。 */
export async function soundboardImport(
  file: File,
): Promise<{ ok: boolean; id: string; duration_s: number }> {
  const fd = new FormData()
  fd.append("file", file)
  return jsonFetch("/soundboard/import", { method: "POST", body: fd })
}

/** 删除**导入**的素材（出厂与包内素材服务端会拒）。 */
export async function soundboardDelete(id: string): Promise<{ ok: boolean; id: string }> {
  return jsonFetch(`/soundboard/${encodeURIComponent(id)}`, { method: "DELETE" })
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

export type HistoryKind =
  | "tts"
  | "offlinevc"
  | "audiobook"
  | "fx"
  | "trial"
  | "mine"
  | "seedvc"
  | "cover";

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

// ---- 桌面人偶市场（皮肤市场） ----

export type PetSkinItem = {
  id: string;
  name: string;
  category?: string;
  license?: string;
  attribution?: string;
  description?: string;
  source_type?: string;
  bundle?: boolean;
  size_hint_mb?: number;
};

export type PetInstalledItem = {
  id: string;
  name: string;
  category?: string;
  license?: string;
  attribution?: string;
  applied: boolean;
  preview?: string;
};

export type PetSkinState = { sheet: string; frames: number; dur: number };
export type PetAppliedSkin = {
  id: string;
  frameW: number;
  frameH: number;
  states: Record<string, PetSkinState>;
};

export type PetTaskItem = {
  skin_id: string;
  status: string;   // queued | downloading | installing | done | failed | cancelled | idle
  phase: string;
  message: string;
  percent: number;
  error: string;
};
export type PetProgress = {
  items: PetTaskItem[];
  active: number;   // 进行中（下载/转换）任务数
  queued: number;   // 排队任务数
};
export type PetSkinDetail = {
  item: {
    id: string;
    name: string;
    category?: string;
    license?: string;
    attribution?: string;
    description?: string;
    source_type?: string;
    bundle?: boolean;
  };
  source_urls: string[];
  installed: boolean;
  applied: boolean;
  frameW: number;
  frameH: number;
  states: Record<string, PetSkinState>;
  license_text: string;
};
export type PetSearchItem = PetSkinItem & { installed: boolean; applied: boolean };

export async function petManifest(): Promise<PetSkinItem[]> {
  const data = await jsonFetch<{ items: PetSkinItem[] }>("/pet-market/manifest");
  return data.items;
}

export async function petSearch(q: string, cat: string): Promise<PetSearchItem[]> {
  const data = await jsonFetch<{ items: PetSearchItem[] }>(
    `/pet-market/search?q=${encodeURIComponent(q)}&cat=${encodeURIComponent(cat)}`,
  );
  return data.items;
}

export async function petDetail(skin_id: string): Promise<PetSkinDetail> {
  return jsonFetch<PetSkinDetail>(`/pet-market/detail/${encodeURIComponent(skin_id)}`);
}

export async function petInstalled(): Promise<PetInstalledItem[]> {
  const data = await jsonFetch<{ items: PetInstalledItem[] }>("/pet-market/installed");
  return data.items;
}

export async function petApplied(): Promise<PetAppliedSkin> {
  return jsonFetch<PetAppliedSkin>("/pet-market/applied");
}

export async function petProgress(): Promise<PetProgress> {
  return jsonFetch<PetProgress>("/pet-market/progress");
}

export async function petInstall(skin_id: string): Promise<PetTaskItem> {
  return jsonFetch("/pet-market/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

export async function petCancel(skin_id: string): Promise<PetTaskItem> {
  return jsonFetch("/pet-market/cancel", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

export async function petApply(skin_id: string): Promise<{ applied: string }> {
  return jsonFetch("/pet-market/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

export async function petUninstall(skin_id: string): Promise<{ uninstalled: string; reset_applied?: boolean }> {
  return jsonFetch("/pet-market/uninstall", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skin_id }),
  });
}

// ---- GitHub 扫描器（发现 Tab） ----

export type PetScanProgress = {
  status: string;      // idle | running | done | failed | cancelled
  phase: string;
  step: string;
  total: number;
  current: number;
  repos_seen: number;
  repos_lic_skip: number;
  repos_tree_skip: number;
  candidates: number;
  built_ok: number;
  built_fail: number;
  atlas_skip: number;
  error: string;
  finished_at: string;
  fails: Array<{ repo: string; path: string; reason: string }>;  // 试转失败原因日志
};
export type PetDiscoveryItem = {
  id: string;
  name: string;
  category?: string;
  license?: string;
  attribution?: string;
  description?: string;
  source_type?: string;
  installed: boolean;
  discovery: {
    repo: string;
    stars: number;
    license: string;
    kind: string;
    path: string;
    count: number;
    scanned_at: string;
  };
};

export async function petScanStart(): Promise<PetScanProgress> {
  return jsonFetch("/pet-market/scan", { method: "POST" });
}

export async function petScanProgress(): Promise<PetScanProgress> {
  return jsonFetch("/pet-market/scan/progress");
}

export async function petScanCancel(): Promise<PetScanProgress> {
  return jsonFetch("/pet-market/scan/cancel", { method: "POST" });
}

export async function petDiscovery(): Promise<PetDiscoveryItem[]> {
  const data = await jsonFetch<{ items: PetDiscoveryItem[] }>("/pet-market/discovery");
  return data.items;
}

export async function petDiscoveryRemove(skin_id: string): Promise<{ removed: string }> {
  return jsonFetch(`/pet-market/discovery/${encodeURIComponent(skin_id)}`, { method: "DELETE" });
}

// ---- 试音间（一个声音 × 多个音色）----

/** 试音前的环境态势：谁在占 GPU、显存余量、能不能开跑（前端据此禁用按钮并给原因） */
export type AuditionEnv = {
  live_running: boolean;
  live_exp: string;
  cascade_running: boolean;
  offline_running: boolean;
  tts_worker: boolean;
  gpu_total_mb: number | null;
  gpu_used_mb: number | null;
  gpu_free_mb: number | null;
  min_free_vram_mb: number;
  low_vram: boolean;
  /** 非空即表示 GPU 被占用，字符串就是可读原因（直接展示给用户） */
  busy_reason: string;
  batch_ready: boolean;
  text_ready: boolean;
  /** 常驻换声引擎：alive 时批量试音快一个数量级（换模型仅 ~0.6s） */
  rvc_worker: { enabled: boolean; alive: boolean; pid: number | null };
};

export type AuditionSource = {
  source_id: string;
  url: string;
  duration_s: number;
  builtin?: boolean;
  created_at?: number;
};

export type AuditionResult = {
  voice_id: string;
  display_name: string;
  status: "running" | "done" | "failed";
  url: string;
  /** 音色像度（CAM++ 声纹余弦）；音色无参考音时为 null —— 不是 0，是"算不了" */
  secs: number | null;
  /** 自然度（NatScore）；打分器缺失时为 null */
  nats: number | null;
  duration_s: number | null;
  error: string;
  score_error: string;
  /** 命中缓存：同样参数之前跑过，秒出没重跑推理 */
  from_cache: boolean;
  /** 实际走的引擎：cache / tts / worker（常驻，快）/ subprocess（回退，慢） */
  engine: "cache" | "tts" | "worker" | "subprocess" | "";
};

export type AuditionTask = {
  task_id: string;
  /** 推理阶段在跑（打分阶段不影响这个值） */
  running: boolean;
  status: "idle" | "running" | "done" | "cancelled" | "error";
  mode: "audio" | "text" | "";
  total: number;
  finished: number;
  current: string;
  current_name: string;
  message: string;
  error: string;
  source_name: string;
  text: string;
  results: AuditionResult[];
  /** 结果都出来后，客观分还在后台算（不挡结果，也不占独占位） */
  scoring: boolean;
  score_finished: number;
  score_total: number;
};

export async function auditionEnv(): Promise<AuditionEnv> {
  return jsonFetch("/audition/env");
}

export async function auditionSources(): Promise<AuditionSource[]> {
  const data = await jsonFetch<{ sources: AuditionSource[] }>("/audition/sources");
  return data.sources;
}

export async function auditionUploadSource(file: Blob, filename = "a.wav"): Promise<AuditionSource> {
  const fd = new FormData();
  fd.append("file", file, filename);
  return jsonFetch("/audition/source", { method: "POST", body: fd });
}

export async function auditionBuiltinSource(): Promise<AuditionSource> {
  return jsonFetch("/audition/source/builtin", { method: "POST" });
}

export async function auditionDeleteSource(sourceId: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/audition/source/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
}

export async function auditionTry(req: {
  voice_ids: string[];
  source_id?: string;
  text?: string;
  pitch?: number;
  index_rate?: number;
  /** 是否计算客观分（音色像度 / 自然度）；关掉省一次打分器加载 */
  score?: boolean;
}): Promise<{ ok: boolean; task_id: string; total: number; mode: string; score: boolean }> {
  return jsonFetch("/audition/try", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function auditionTask(): Promise<AuditionTask> {
  return jsonFetch("/audition/task");
}

export async function auditionCancel(): Promise<{ ok: boolean; cancelled: boolean; message?: string }> {
  return jsonFetch("/audition/cancel", { method: "POST" });
}

// ---------------------------------------------------------------- 在线扒歌（yt-dlp）

/** yt-dlp 这个**外部工具**的可用状态（`/ytdlp/status`，只读、不发网络）。 */
export type YtdlpStatus = {
  /** 找没找到可执行文件。false 时 `hint` 是"怎么装"的人话。 */
  available: boolean;
  path: string;
  version: string;
  /** 放行的站点（本插件**只**支持这些，不是 yt-dlp 的全量清单） */
  sites: { name: string; example: string }[];
  /**
   * 登录态配置。QQ 音乐这类站点不给登录态就只返"要登录"，所以这一项要能看见
   * —— 用户配完 cookies 得有个地方确认自己配对了。`mode: "none"` = 发匿名请求。
   */
  cookie: { mode: "none" | "file" | "browser"; detail: string; ok: boolean };
  /**
   * 会员曲兜底（QQ 音乐 MV 抽音轨）。**默认关**，由后端环境变量 `VM_MV_FALLBACK`
   * 控制。开着时，yt-dlp 拿不到音频（会员曲）会自动改从官方 MV 抽音轨，
   * 代价是音质打折 —— 所以这一项要显示出来，让用户知道自己在走哪条路。
   */
  mv_fallback?: {
    enabled: boolean;
    env: string;
    /** 找到的 playwright-core 路径（空串 = 没找到，开着也用不了） */
    playwright_core: string;
    node: string;
    /** 人话状态（"已开启：…" / "未开启。设 … 后重启后端"） */
    detail: string;
  };
  hint: string;
};

/** 扒一首歌的结果（`/ytdlp/fetch`）：name 是会话里的裸名，url 可直接试听 */
export type YtdlpFetchResult = {
  ok: boolean;
  name: string;
  url: string;
  bytes: number;
  /** 命中的站点显示名（如"网易云音乐"），用来在 UI 上确认解析对了 */
  site: string;
  /**
   * 实际交给 yt-dlp 的地址。用户粘的是短链时，这里是被**规范化**之后的规范地址
   * —— 排查"这条链接为什么不行"时，这一项比任何日志都直接。
   */
  source_url: string;
  duration_s: number;
  /**
   * 这份音频**是怎么来的**。
   *
   * - `ytdlp`：正版音源（默认路径）
   * - `mv_fallback`：QQ 音乐会员曲的兜底 —— 从官方 MV 抽的音轨
   *
   * 必须让用户看出差别：兜底产物是混过影像声音的 192kbps AAC，不是母带。
   */
  via?: "ytdlp" | "mv_fallback";
  /**
   * 只在 `via === "mv_fallback"` 时出现：音质打折的说明。
   * UI **必须**把它显示出来，不能让兜底产物看起来和正版音源一样。
   */
  quality_note?: string;
};

/**
 * 查 yt-dlp 状态。**不发网络请求**，页面挂载时可直接调。
 *
 * 端点属 `sound.ytdlp`（可关插件）：关掉后 `/ytdlp/*` 不再挂载，
 * 调用方应先 `pluginVisible(catalog, "sound.ytdlp")` 门控，别裸调。
 */
export async function ytdlpStatus(): Promise<YtdlpStatus> {
  return jsonFetch<YtdlpStatus>("/ytdlp/status");
}

/** 取回过程中的**作业状态**（`/ytdlp/fetch/{job_id}`）。 */
export type YtdlpJobState = {
  /** 作业还在不在。false = 跑完了 / 已取消 / id 不对 —— 不是错误。 */
  found: boolean;
  /** 人话阶段（"解析页面" / "下载中" / "转码中"…），拿不到就是空串 */
  stage: string;
  /** 下载百分比；yt-dlp 没吐进度行时是 null（别显示成 0%） */
  percent: number | null;
  cancelled: boolean;
  elapsed_s: number;
};

/**
 * 粘一条平台分享链接 → 交给外部 yt-dlp 把音频拉到会话目录（**只下载，不跑链路**）。
 *
 * 拉回来的产物就在会话目录里，退出即删；可以直接用它的 `name` 作为
 * `/cover/run` 的 `src_name` 去跑翻唱，不必先下到本地中转。
 *
 * 失败（站点不在白名单 / 没装 yt-dlp / 歌曲要登录）都返回 400 且 `detail` 是人话。
 *
 * `jobId` 让这次取回**可取消**：后端按它登记一张作业票，`ytdlpCancel` 或
 * `ytdlpJob` 都按同一个 id 找它。`signal` 让调用方还能在浏览器这一侧 abort
 * （后端也会因为连接断开而把 yt-dlp 停掉，两条路都通）。
 */
export async function fetchYtdlp(
  url: string,
  jobId = "",
  signal?: AbortSignal,
): Promise<YtdlpFetchResult> {
  return jsonFetch<YtdlpFetchResult>("/ytdlp/fetch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(jobId ? { url, job_id: jobId } : { url }),
    signal,
  });
}

/**
 * 查这次取回到哪一步了（**只读内存**，不碰网络、不碰 yt-dlp）。
 *
 * 取回是同步阻塞的，前端拿不到流式进度，所以按秒轮询这一条 —— 有了它，
 * "这条链接是不是卡住了"用户自己就能判断，不用靠干等。
 */
export async function ytdlpJob(jobId: string): Promise<YtdlpJobState> {
  return jsonFetch<YtdlpJobState>(`/ytdlp/fetch/${encodeURIComponent(jobId)}`);
}

/**
 * 喊停这次取回。返回 `found: false` 表示作业已经不在了（跑完了 / id 不对），
 * **不是错误** —— 用户在一个刚跑完的作业上点取消是完全正常的动作。
 */
export async function ytdlpCancel(jobId: string): Promise<{ ok: boolean; found: boolean }> {
  return jsonFetch<{ ok: boolean; found: boolean }>(
    `/ytdlp/fetch/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
}

// ---- 声音保存（sound.preserve）----
//
// 「在声音还清晰时录一段、本机克隆保存，日后以自己的声音输出」。
// ★ 前端**不实现合成**：复现走现有 TTS / 发送链路，这里只负责
//   「档案管理 + 录音上传 + 知情同意 + 门禁体检 + 给出该用哪段参考音」。
//   所以下面没有"合成"相关的函数 —— 那是 useTts / useWechatSend 的活。

export type PreserveRef = {
  duration_s: number
  tag: string
  file: string
  /** 实际时长。**与 `duration_s`（标称档）分开记** —— 报结论要用实际值。 */
  actual_s: number | null
  bytes: number
  recorded_at: string
}

export type PreserveConsent = {
  version: string
  scope: string
  statement: string
  operator: string
  agreed_at: string
}

export type PreserveProfile = {
  id: string
  label: string
  note: string
  created_at: string
  updated_at: string
  consent: PreserveConsent | null
  refs: PreserveRef[]
  truth: { condition: string; file: string }[]
}

export type PreserveProfileSummary = {
  id: string
  label: string
  created_at: string
  consent_ok: boolean
  n_refs: number
  max_duration_s: number | null
}

export type PreserveLamp = { label: string; lamp: string | null; detail: string }

export type PreserveGate = {
  verdict: "ready" | "partial" | "not_ready"
  /** 不能用的原因，**分因**（不是"出错了"这种套话）。 */
  reasons: string[]
  consent_ok: boolean
  refs: PreserveRef[]
  max_duration_s: number | null
  /** 三灯：**仅供参考，不参与 verdict**（阈值锚在另一批素材上，见后端注释）。 */
  lamps: { lamps: PreserveLamp[]; verdict: string } | null
  metrics: Record<string, number | null> | null
}

export type PreserveGuide = {
  script: string[]
  script_chars: number
  script_capacity_s: number
  durations: { duration_s: number; tag: string; sentences: number; covered: boolean }[]
  repro_texts: Record<string, { text: string; style: string }[]>
  conditions: string[]
  consent: { version: string; statement: string; scopes: string[] }
}

export type PreserveEvent = { at: string; kind: string; [k: string]: unknown }

export async function getPreserveGuide(): Promise<PreserveGuide> {
  return jsonFetch("/preserve/guide")
}

export async function getPreserveSummary(): Promise<{
  dir: string
  count: number
  with_consent: number
  with_refs: number
  profiles: PreserveProfileSummary[]
}> {
  return jsonFetch("/preserve/summary")
}

export async function listPreserveProfiles(): Promise<{ profiles: PreserveProfileSummary[] }> {
  return jsonFetch("/preserve/profiles")
}

export async function createPreserveProfile(
  label: string,
  note = ""
): Promise<{ ok: boolean; profile: PreserveProfile }> {
  return jsonFetch("/preserve/profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, note }),
  })
}

export async function getPreserveProfile(id: string): Promise<{
  profile: PreserveProfile
  events: PreserveEvent[]
  events_total: number
}> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id))
}

export async function deletePreserveProfile(id: string): Promise<{ ok: boolean; deleted: string }> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id), { method: "DELETE" })
}

export async function recordPreserveConsent(
  id: string,
  body: { scope?: string; statement?: string; operator?: string }
): Promise<{ ok: boolean; consent: PreserveConsent }> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/consent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

/** 门禁体检。**会真的算指标**（约 1 秒），所以是 POST —— 与三灯的 GET 只读/POST 现算同约定。 */
export async function runPreserveGate(id: string): Promise<PreserveGate> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/gate", { method: "POST" })
}

export async function getPreserveEvents(id: string, limit = 200): Promise<{ events: PreserveEvent[] }> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/events?limit=" + limit)
}

/**
 * 上传一档参考音。
 *
 * 前端录出来的是 **webm/opus**（MediaRecorder）—— 后端会用 ffmpeg 归一成
 * 单声道 wav 再入库。别在前端自己改后缀，那只会造出「后缀对、内容错」的文件。
 */
export async function uploadPreserveReference(
  id: string,
  file: File,
  durationS: number
): Promise<{ ok: boolean; reference: PreserveRef }> {
  const form = new FormData()
  form.append("file", file)
  form.append("duration_s", String(durationS))
  // ⚠️ 这里**必须内联 `fetch(BASE + 字面量)`**，不能抽成 postForm(url, form) 之类的辅助函数：
  //   `tools/audit_endpoint_ownership.py` 靠 AST 从调用点抽出 URL 字面量来判断这个端点
  //   属于哪个插件；URL 一旦变成参数，它就抽不到 → `[gate]` 报「未定位函数」。
  //   （仓库里其它 multipart 上传也都是内联的，同一个原因。）
  const res = await fetch(BASE + "/preserve/profiles/" + encodeURIComponent(id) + "/references", {
    method: "POST",
    body: form,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<{ ok: boolean; reference: PreserveRef }>
}

/**
 * 上传**一条完整录音**，后端按前缀自动切出全部时长档（3/10/30/60s）。
 *
 * ★ 与 `uploadPreserveReference`（单档）的区别是**研究语义**，不只是省请求：
 *   评测协议把四档定义为「同一段录音的前缀截取」，控制变量要求"同一条 take"。
 *   逐档上传 = 四条不同 take（嗓子/坐姿/距离都会变），
 *   自变量"时长"就被"这次录得好不好"混淆 → RQ1 归因不成立。
 *
 * 录音不足最高档（60s）时后端返回 400 并说明差多少，且不留下半套数据。
 */
export async function uploadPreserveReferenceBatch(
  id: string,
  file: File
): Promise<{ ok: boolean; tags: string[]; total_s: number; refs: PreserveRef[] }> {
  const form = new FormData()
  form.append("file", file)
  // ⚠️ 同 `uploadPreserveReference`：**必须内联 `fetch(BASE + 字面量)`** ——
  //   `tools/audit_endpoint_ownership.py` 靠 AST 从调用点抽 URL 字面量判断端点归属。
  const res = await fetch(
    BASE + "/preserve/profiles/" + encodeURIComponent(id) + "/references/batch",
    { method: "POST", body: form }
  )
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<{
    ok: boolean
    tags: string[]
    total_s: number
    refs: PreserveRef[]
  }>
}

export async function uploadPreserveTruth(
  id: string,
  file: File,
  condition: string
): Promise<{ ok: boolean; truth: { condition: string; file: string } }> {
  const form = new FormData()
  form.append("file", file)
  form.append("condition", condition)
  const res = await fetch(BASE + "/preserve/profiles/" + encodeURIComponent(id) + "/truth", {
    method: "POST",
    body: form,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<{ ok: boolean; truth: { condition: string; file: string } }>
}

/** 取一档参考音（省略 durationS 取最长档）。拿到 path/url 后交给现有 TTS 链路。 */
export async function getPreserveReference(
  id: string,
  durationS?: number
): Promise<{ path: string; url: string; file: string; duration_s: number | null }> {
  const q = durationS == null ? "" : "?duration_s=" + durationS
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/reference" + q)
}

/** 「去复现」：把档案的最长档参考音注册成音色库音色并设为当前选中。
 *  没有知情同意时后端返回 400（同意先于克隆）。幂等：重录后再调会刷新参考音。 */
export async function reproducePreserveProfile(
  id: string
): Promise<{ ok: boolean; voice_id: string; label: string; ref: string; duration_s: number | null }> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/reproduce", {
    method: "POST",
  })
}

/** 导出成评测实验目录（产品层 → 研究层的桥）。
 *
 *  为什么必须有它：档案存 media/preserve/<pid>/，而评测引擎读
 *  outputs/voicebank/<exp>/ —— 两边目录约定对不上，中间原本只能手工拷文件，
 *  而手工拷漏一档/拷错档**都不报错**（前者让 analyze 报"数据不足以判定"，
 *  后者直接把自变量标错）。这里按结构拷，档位名从档案记录现读。
 *
 *  没有知情同意时后端返回 400（数据离开档案库之前必须有授权）。幂等。 */
export async function exportPreserveProfile(
  id: string,
  exp = ""
): Promise<{
  ok: boolean
  exp: string
  root: string
  n_refs: number
  n_truth: number
  missing: string[]
  measure_hint: string
}> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ exp }),
  })
}

/** 一次「声音随访」的趋势点（后端 `voicepreserve.add_followup` 落库的那行记录）。 */
export type PreserveFollowupPoint = {
  at: string
  at_ms: number
  file: string
  /** 可回听 URL（后端补的；补不上是空串）。 */
  url?: string
  actual_s: number | null
  bytes: number
  note: string
  /** 指标算不出来时是 null（如实降级，不是数据坏了 —— 与 gate 的 partial 同哲学）。 */
  metrics: Record<string, number | null> | null
  lamps: { lamps: PreserveLamp[]; verdict: string } | null
  /** 与基线参考音的声纹余弦。worker 没跑 / 没基线时是 null（相似度灯显示未测）。 */
  emb_sim: number | null
  metrics_error?: string
}

export type PreserveTrend = {
  n: number
  points: PreserveFollowupPoint[]
  /** 措辞红线的落点：这句文案前后端同守「不出现诊断 / 健康监测 / 疗效」。 */
  hint: string
}

/** 追加一次「声音随访」（后端会算指标，约 1 秒，所以是 POST）。追加不替换。 */
export async function uploadPreserveFollowup(
  id: string,
  file: File,
  note = ""
): Promise<{ ok: boolean; followup: PreserveFollowupPoint }> {
  const form = new FormData()
  form.append("file", file)
  if (note) form.append("note", note)
  // ⚠️ 同 `uploadPreserveReference`：**必须内联 `fetch(BASE + 字面量)`** ——
  //   `tools/audit_endpoint_ownership.py` 靠 AST 从调用点抽 URL 字面量判断端点归属。
  const res = await fetch(BASE + "/preserve/profiles/" + encodeURIComponent(id) + "/followup", {
    method: "POST",
    body: form,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) detail = body.detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<{ ok: boolean; followup: PreserveFollowupPoint }>
}

/** 三灯趋势（只读，不重算）。口径是「发声状态趋势参考」，不是医疗诊断。 */
export async function getPreserveTrend(id: string): Promise<PreserveTrend> {
  return jsonFetch("/preserve/profiles/" + encodeURIComponent(id) + "/trend")
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

// ---- 真声直讲（真声原样灌进虚拟声卡，2026-10-04） ----
//
// 场景：老师用电脑播 PPT + 电脑麦克风讲课，**平时是本人真声**，讲累了按热键
// 插一句 AI 音 —— 学生那头听到连续混音，不切换、不打断。
//
// ★ 用户在**外部软件**里还要做一步（界面必须提示，否则会被当成坏了）：
//   把 QQ / 微信 / 钉钉的**麦克风**设为 `CABLE Output (VB-Audio Virtual Cable)`。

/** 真声直讲的状态。`device` 是声音正送往哪（CABLE 渲染端），`inputDevice` 是真麦。 */
export interface PassthroughStatus {
  ok: boolean;
  running: boolean;
  device: string;
  input_device: string;
}

export async function passthroughStatus(): Promise<PassthroughStatus> {
  const d = await jsonFetch<{ ok: boolean; running: boolean; device?: string; input_device?: string }>(
    "/passthrough/status"
  );
  return { ok: d.ok, running: d.running, device: d.device ?? "", input_device: d.input_device ?? "" };
}

/**
 * 开启真声直讲。与实时变声 / 级联**互斥** —— 三者都占虚拟声卡出口，
 * 冲突时后端返回 409（调用方按普通错误提示即可）。
 */
export async function passthroughStart(): Promise<{ ok: boolean; running: boolean; device: string }> {
  const d = await jsonFetch<{ ok: boolean; running?: boolean; device?: string }>(
    "/passthrough/start",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
  );
  return { ok: d.ok, running: d.running ?? true, device: d.device ?? "" };
}

/** 关闭真声直讲（worker 退出时自动关流、释放设备）。 */
export async function passthroughStop(): Promise<{ ok: boolean; running: boolean }> {
  const d = await jsonFetch<{ ok: boolean; running?: boolean }>("/passthrough/stop", {
    method: "POST",
  });
  return { ok: d.ok, running: d.running ?? false };
}

// ---- 音频溯源水印（sound.watermark） ----
//
// 基于 Meta AudioSeal（MIT）：给音频嵌一个隐形的 16 位溯源码（4 位 hex），
// 听感不变；也能检测一段音频带没带我们的码。★ 口径红线（与后端一致）：
// 只说「可检出 / 可溯源」，不说「防伪 / 无法去除」—— 隐形水印是概率性取证信号。

/** 单个权重下载任务（沿用市场下载器的进度结构；total/done 为字节） */
export type WatermarkDlTask = {
  name?: string;
  filename?: string;
  status?: string; // downloading | done | failed | cancelled | idle
  total?: number | null;
  done?: number;
  error?: string;
  [k: string]: unknown;
};

export type WatermarkStatus = {
  models_dir: string;
  installed: boolean;
  size_hint_mb: number;
  download: { generator: WatermarkDlTask; detector: WatermarkDlTask };
  auto: { enabled: boolean; code: string };
};

export type WatermarkEmbedResult = {
  ok: boolean;
  code: string;
  sr: number;
  duration_s: number;
  /** 会话产物裸名（outputs/ 下）；退出即删 */
  wav: string;
  url: string;
};

export type WatermarkDetectResult = {
  ok: boolean;
  watermarked: boolean;
  prob: number;
  code: string;
  threshold: number;
};

export async function watermarkStatus(): Promise<WatermarkStatus> {
  return jsonFetch<WatermarkStatus>("/watermark/status");
}

/** 下载 AudioSeal 权重（约 90MB，两文件）。幂等：已装好回 done；在下回 downloading，前端轮询 status。 */
export async function watermarkDownload(): Promise<{
  status: string;
  installed?: boolean;
  tasks?: string[];
}> {
  return jsonFetch("/watermark/download", { method: "POST" });
}

/**
 * 植入水印 → 会话产物。file=本地文件；或 name=会话/产物裸名（如刚合成的语音）。
 * code 留空则后端随机生成（返回值里带回，用户要记下它）。
 */
export async function watermarkEmbed(
  input: { file: File } | { name: string },
  code = "",
  alpha = 1.0
): Promise<WatermarkEmbedResult> {
  const form = new FormData();
  if ("file" in input) form.append("file", input.file);
  else form.append("name", input.name);
  form.append("code", code);
  form.append("alpha", String(alpha));
  // ⚠️ 必须内联 `fetch(BASE + 字面量)`：tools/audit_endpoint_ownership.py 靠 AST
  //   从调用点抽 URL 字面量判断端点归属（同 uploadPreserveReference 的注释）。
  const res = await fetch(BASE + "/watermark/embed", { method: "POST", body: form });
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
  return res.json() as Promise<WatermarkEmbedResult>;
}

/** 检测一段音频带没带我们的溯源码。 */
export async function watermarkDetect(
  input: { file: File } | { name: string }
): Promise<WatermarkDetectResult> {
  const form = new FormData();
  if ("file" in input) form.append("file", input.file);
  else form.append("name", input.name);
  const res = await fetch(BASE + "/watermark/detect", { method: "POST", body: form });
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
  return res.json() as Promise<WatermarkDetectResult>;
}

export async function watermarkAutoGet(): Promise<{ enabled: boolean; code: string }> {
  return jsonFetch("/watermark/auto");
}

/** 「合成自动加标识」开关：开着时所有合成产物（含微信语音）自动嵌入该码。默认关。 */
export async function watermarkAutoSet(
  enabled: boolean,
  code = ""
): Promise<{ ok: boolean; enabled: boolean; code: string }> {
  return jsonFetch("/watermark/auto", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, code }),
  });
}

// ---- 盲听评测（sound.blindtest） ----

export type BlindtestSessionSummary = {
  id: string;
  title: string;
  created_at: number;
  closed: boolean;
  n_samples: number;
  n_responses: number;
};

export type BlindtestSampleRow = {
  sid: string;
  label: string;
  order: number;
  n: number;
  mean: number | null;
  std: number | null;
  min: number | null;
  max: number | null;
  rank?: number;
};

export type BlindtestResponse = {
  at: number;
  ua: string;
  scores: Record<string, number>;
  note: string;
};

export type BlindtestDetail = {
  id: string;
  title: string;
  created_at: number;
  closed: boolean;
  token: string;
  samples: { sid: string; label: string; order: number }[];
  aggregate: BlindtestSampleRow[];
  responses: BlindtestResponse[];
};

export async function blindtestSessions(): Promise<{ sessions: BlindtestSessionSummary[] }> {
  return jsonFetch("/blindtest/sessions");
}

/** 建盲听会话：上传样本（可混 outputs/ 相对路径引用），labels 与样本一一对应。 */
export async function blindtestCreate(
  title: string,
  labels: string[],
  files: File[],
  refs: string[]
): Promise<{ ok: boolean; id: string; token: string }> {
  const form = new FormData();
  form.append("title", title);
  form.append("labels", JSON.stringify(labels));
  form.append("refs", JSON.stringify(refs));
  for (const f of files) form.append("files", f);
  const res = await fetch(BASE + "/blindtest/sessions", { method: "POST", body: form });
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
  return res.json() as Promise<{ ok: boolean; id: string; token: string }>;
}

export async function blindtestDetail(id: string): Promise<BlindtestDetail> {
  return jsonFetch(`/blindtest/sessions/${encodeURIComponent(id)}`);
}

export async function blindtestClose(id: string, closed: boolean): Promise<{ ok: boolean }> {
  return jsonFetch(`/blindtest/sessions/${encodeURIComponent(id)}/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ closed }),
  });
}

export async function blindtestDelete(id: string): Promise<{ ok: boolean }> {
  return jsonFetch(`/blindtest/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** 试音间当前任务的已完成结果（导入盲听样本用；试音间关着/没跑过 → 抛错由调用方兜）。 */
export type AuditionPick = { voice_id: string; display_name: string; url: string };

export async function auditionPicks(): Promise<AuditionPick[]> {
  const task = await jsonFetch<{
    results: { status: string; voice_id: string; display_name: string; url: string }[];
  }>("/audition/task");
  return (task.results || [])
    .filter((r) => r.status === "done" && r.url)
    .map((r) => ({ voice_id: r.voice_id, display_name: r.display_name, url: r.url }));
}
