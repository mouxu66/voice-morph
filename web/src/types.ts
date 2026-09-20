// 与 FastAPI 后端(m2_server)交互的数据类型

export interface HealthInfo {
  status: string;
  cuda: boolean;
}

/** 一个加载失败的后端能力（路由模块 / 启动钩子）—— 对应 `/api/capabilities` 的 broken 项 */
export interface CapabilityBroken {
  module: string;
  purpose: string;
  reason: string;
}

/**
 * 后端能力加载清单（`GET /api/capabilities`）。
 *
 * 为什么单独一条而不并进 HealthInfo：判别的是两件事 ——
 * `/health` 答「服务在不在」，这里答「服务里有哪些能力真的挂上了」。
 * 这些能力「用户没装」是正常状态（缺 torch / 缺模型 / 没装微信），
 * 不该表现为「软件坏了」，但也不该静默 —— 所以要在界面上说清楚。
 */
export interface CapabilityInfo {
  ok: boolean;
  loaded: number;
  total: number;
  broken: CapabilityBroken[];
}

/** 能力声明的导航信息（`plugin.json` 的 `routes[].nav`）—— 侧边栏由它渲染 */
export interface PluginNav {
  label: string;
  icon?: string;
  group: 'start' | 'more';
  order: number;
}

/**
 * `plugin.json` 里的一条页面路由。
 *
 * `module` / `export` 都是必填：本仓页面**全是具名导出、零 default export**
 * （`Home/index.tsx` → `HomeRoute`），所以必须指名取哪个导出。
 */
export interface PluginRoute {
  path: string;
  module: string;
  export: string;
  nav?: PluginNav;
}

/** 只做重定向的旧路由：页面合并后留下的入口（如 `/discover` → `/workshop?tab=discover`） */
export interface PluginLegacyRoute {
  path: string;
  redirect: string;
}

/** 一个能力要用到、但不在 pip 里的东西（本机目录 / 应用 / 音频设备） */
export interface PluginExternalAsset {
  kind: 'dir' | 'app' | 'audio-device';
  label: string;
  /** 指向该资源的环境变量（如 `VM_RVC_ROOT`） */
  env?: string;
  size_hint_mb?: number;
  /** 相关文档位置（这个模块的坑记在哪） */
  doc?: string;
}

/**
 * 需要另外下载的权重 / 模型目录。
 *
 * 与 `external` 的区别：那是「要装的软件」，这是「要下的东西」。
 * ⚠️ 元素是**对象**不是字符串 —— 这里曾按 `string[]` 声明，界面上就渲染成了
 * `[object Object]`（只有真机截图才看得出来，单测用的是手写数据所以没暴露）。
 */
export interface PluginModelAsset {
  label: string;
  /** 指向该模型目录的环境变量（如 `VM_TTS_MODELS_DIR`） */
  env?: string;
  size_hint_mb?: number;
}

/** 一个能力要装的东西；`python` 是 pip 包名，另两项是对象数组 */
export interface PluginExtras {
  python?: string[];
  external?: PluginExternalAsset[];
  models?: PluginModelAsset[];
}

/**
 * 三态。关键是 `disabled`（用户主动关的）**不算 broken** ——
 * 否则关一个能力就会弹一条「N 个能力未加载」的降级横幅。
 */
export type PluginState = 'ok' | 'broken' | 'disabled';

/** 插件目录里的一项能力（`GET /api/plugins` 的 `plugins[i]`） */
export interface PluginEntry {
  id: string;
  name: string;
  kind: 'builtin' | 'user';
  category: 'core' | 'sound' | 'pet' | 'hook';
  order: number;
  summary: string;
  /** 核心能力不可关（关掉它整个界面就没有意义了） */
  core: boolean;
  state: PluginState;
  /** 加载失败原因。`disabled` 的项**也会带着** —— 设置页要能说「你关的，而且它本来就是坏的」 */
  reasons: string[];
  /** 依赖的其它能力 id；只做存在性 + 无环校验，暂不做状态传播 */
  requires: string[];
  routers: string[];
  routes: PluginRoute[];
  legacyRoutes: PluginLegacyRoute[];
  extras: PluginExtras;
  health: { module: string; attr: string } | null;
  /** 为什么不能关 / 关了会失去什么 */
  disableNote: string;
}

/**
 * 能力清单（`GET /api/plugins`）。
 *
 * 与 `/capabilities` 的分工：那边是**加载器**的原始账本（26 个 router 逐个成功/失败），
 * 这里是**能力**视角（哪些可用、哪些被关掉、每个要装什么）。`loaders` 把两边的账
 * 一起报出来 —— 对不上时能一眼看出问题出在哪一层。
 *
 * **只读**：开关能力是插件化第 6 步的事（`POST /api/plugins/{id}/enable|disable`）。
 */
export interface PluginCatalog {
  ok: boolean;
  counts: { total: number; ok: number; broken: number; disabled: number };
  plugins: PluginEntry[];
  loaders: { routers: number; loaded: number; broken: string[] };
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
