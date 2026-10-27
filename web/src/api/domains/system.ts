import type { CapabilityInfo, DiagnoseInfo, HealthInfo, LanInfo, PluginCatalog, PluginPresetResult, PluginToggleResult, SendChainInfo, TtsChainInfo, VoiceInfo, WarmupStatus } from "../../types";
import { jsonFetch, BASE } from "../http";

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
 * 而 `/diagnose` 只答「装没装」。设计见 `docs/交接-AI必读.md`。
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
