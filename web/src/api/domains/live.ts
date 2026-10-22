import { jsonFetch } from "../http";

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
