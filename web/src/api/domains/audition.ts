import { jsonFetch } from "../http";

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
