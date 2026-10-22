import { jsonFetch } from "../http";

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
