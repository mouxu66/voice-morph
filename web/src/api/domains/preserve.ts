import { jsonFetch, BASE } from "../http";

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
