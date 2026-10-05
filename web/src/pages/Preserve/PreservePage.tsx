import { useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  AudioLines,
  BookOpen,
  CheckCircle2,
  CircleAlert,
  CircleDashed,
  FileDown,
  Info,
  Loader2,
  Mic,
  ShieldCheck,
  Square,
  Trash2,
  UserRoundPlus,
} from "lucide-react"
import type { usePreserve } from "@/pages/Preserve/usePreserve"
import { setPluginEnabled } from "@/api/client"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

const VERDICT_TEXT: Record<string, { label: string; tone: string; hint: string }> = {
  ready: {
    label: "可以复现",
    tone: "border-emerald-500/40 bg-emerald-500/10 text-emerald-600",
    hint: "同意已确认、录音已就绪，三项客观指标也都正常 —— 随时可以使用。",
  },
  partial: {
    label: "部分可用",
    tone: "border-amber-500/40 bg-amber-500/10 text-amber-600",
    hint: "录音齐全，可以正常保存和使用；只是客观指标这次没算出来（这台电脑缺分析组件）—— 不影响任何功能。",
  },
  not_ready: {
    label: "还不能用",
    tone: "border-red-500/40 bg-red-500/10 text-red-600",
    hint: "还差几步就绪 —— 下面把原因逐条列了出来，照着补就行。",
  },
}

const LAMP_DOT: Record<string, string> = {
  green: "bg-emerald-500",
  yellow: "bg-amber-500",
  red: "bg-red-500",
}

/** 时长档位的人话解释 —— 「3s/10s」对老师是密码，翻成用途才看得懂选哪个。 */
const TIER_HINTS: Record<string, string> = {
  "3s": "快速试试",
  "10s": "推荐 · 日常够用",
  "30s": "更稳",
  "60s": "最完整",
}

function Card({
  title,
  step,
  done,
  children,
}: {
  title: string
  /** 步骤号。可选 —— 「声音随访」这类附属能力不属于主线步骤，不占步骤号。 */
  step?: number
  done?: boolean
  children: React.ReactNode
}) {
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <header className="mb-3 flex items-center gap-2">
        {step != null ? (
          <span
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-medium",
              done ? "bg-emerald-500/15 text-emerald-600" : "bg-muted text-muted-foreground"
            )}
          >
            {done ? <CheckCircle2 className="h-3.5 w-3.5" /> : step}
          </span>
        ) : null}
        <h3 className="text-sm font-medium">{title}</h3>
      </header>
      {children}
    </section>
  )
}

/**
 * 三灯趋势小图（内联 SVG，不引图表库 —— 符合最小依赖约定）。
 *
 * 线 = 数值走势（min–max 归一到图高）；圆点 = 一次随访，颜色取**三灯判定色**
 * （green/yellow/red，与体检页同一套 LAMP_DOT）。缺值点如实跳过 ——
 * 指标算不出来的那次本就不该在图上冒出一个假高度。
 *
 * lampIdx：`voice_report.three_lamps` 固定按「相似度 / 自然度 / 发声负荷」的
 * 顺序构造 lamps 数组 —— 按位置取对应灯，不是按文案匹配。
 */
function Sparkline({
  title,
  unit,
  points,
  lampIdx,
}: {
  title: string
  unit: string
  points: NonNullable<ReturnType<typeof usePreserve>["trend"]>["points"]
  lampIdx: number
}) {
  const W = 260
  const H = 56
  const PAD_X = 6
  const PAD_TOP = 8
  const PAD_BOTTOM = 16
  const vals = points
    .map((pt) => ({ pt, v: lampIdx === 0 ? pt.emb_sim : pt.metrics?.[lampIdx === 1 ? "hnr" : "h1_h2"] ?? null }))
    .filter((d): d is { pt: (typeof points)[number]; v: number } => d.v != null)
  const xs = (i: number) =>
    vals.length <= 1 ? W / 2 : PAD_X + (i * (W - 2 * PAD_X)) / (vals.length - 1)
  const min = vals.length ? Math.min(...vals.map((d) => d.v)) : 0
  const max = vals.length ? Math.max(...vals.map((d) => d.v)) : 0
  const span = max - min
  const ys = (v: number) =>
    span <= 0
      ? PAD_TOP + (H - PAD_TOP - PAD_BOTTOM) / 2
      : PAD_TOP + (1 - (v - min) / span) * (H - PAD_TOP - PAD_BOTTOM)
  const lampColor = (d: (typeof vals)[number]) => {
    const lamp = d.pt.lamps?.lamps?.[lampIdx]?.lamp
    return lamp ? LAMP_DOT[lamp] : "bg-muted-foreground/40"
  }
  return (
    <div className="rounded-md border border-border/60 bg-muted/20 p-2">
      <p className="mb-1 text-[11px] text-muted-foreground">
        {title}
        <span className="ml-1 font-mono text-[10px] opacity-70">{unit}</span>
      </p>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-14 w-full" role="img" aria-label={title}>
        {vals.length > 1 ? (
          <polyline
            points={vals.map((d, i) => `${xs(i)},${ys(d.v)}`).join(" ")}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            className="text-primary/50"
          />
        ) : null}
        {vals.map((d, i) => (
          <circle key={i} cx={xs(i)} cy={ys(d.v)} r="3.5" className={lampColor(d)}>
            <title>{`${d.pt.at}：${d.v}${unit}`}</title>
          </circle>
        ))}
        {vals.length === 0 ? (
          <text x={W / 2} y={H / 2} textAnchor="middle" className="fill-muted-foreground text-[9px]">
            暂无数值
          </text>
        ) : null}
      </svg>
    </div>
  )
}

export function PreservePage({ p }: { p: ReturnType<typeof usePreserve> }) {
  const [label, setLabel] = useState("")
  const [operator, setOperator] = useState("")
  const [showScript, setShowScript] = useState(false)
  const navigate = useNavigate()

  /**
   * 「录课」（`sound.audiobook`）是**可关能力**，默认预设下是关着的。
   *
   * 为什么这一页要读它：老师保存声音的**目的**是把一节课的讲稿念出来，而
   * 「去复现」的落点 `_tts` 只做单段合成 —— 长讲稿得走「录课」那个 tab。
   * 若那个 tab 因为能力关着而不存在，老师就会停在输字页对着单段输入框发呆
   * （闭环断在最后一跳）。所以这里按能力清单决定给「去录一节课」还是
   * 「开启「录课」」—— catalog 没拿到时 `pluginVisible` 按可见处理，不误伤旧后端。
   */
  const { state: catalogState, reload: reloadCatalog } = usePluginCatalog()
  const catalog = catalogState.status === "ready" ? catalogState.catalog : null
  const bookOn = pluginVisible(catalog, "sound.audiobook")
  const [lessonBusy, setLessonBusy] = useState(false)
  const [enablingBook, setEnablingBook] = useState(false)
  const [bookErr, setBookErr] = useState("")

  const hasConsent = Boolean(p.profile?.consent)
  // `refs` 必须在 useMemo 里定型：写成 `p.profile?.refs ?? []` 的话，每次渲染
  // 都是新数组 → 下面 `refTags` 的 useMemo 每帧失效（react-hooks/exhaustive-deps）。
  const refs = useMemo(() => p.profile?.refs ?? [], [p.profile])
  const refTags = useMemo(() => new Set(refs.map((r) => r.tag)), [refs])

  // 最高档（用于「一次录满」）：从后端现算的 durations 里取最大，不写死 60 ——
  // 档位口径是研究自变量，只能有一份定义（`voicepreserve.REF_DURATIONS`）。
  const durations = useMemo(() => p.guide?.durations ?? [], [p.guide])
  const longest = useMemo(
    () => durations.reduce<(typeof durations)[number] | null>(
      (acc, d) => (acc === null || d.duration_s > acc.duration_s ? d : acc),
      null
    ),
    [durations]
  )
  const longestS = longest?.duration_s ?? 60
  const longestTag = longest?.tag ?? "60s"
  const longestSentences = longest?.sentences ?? 20

  /** 「去复现」：注册成音色库音色（后端同时设为当前选中），成功即跳输字页。
   *  显示条件（同意 + 至少一档参考音）只是引导；真正的门禁在后端 ——
   *  就算被拦，错误也会原样显示在这里。 */
  const canReproduce = hasConsent && refs.length > 0
  const goReproduce = async () => {
    const rep = await p.reproduce()
    if (rep) navigate("/tts")
  }

  /** 「去录一节课」：同一个注册动作（都要先成为音色库音色），只是落点换成
   *  「录课」tab —— 老师要的是把整篇讲稿念完，不是合成一句话。
   *  ⚠️ 别把 `goReproduce` 改成带参数的跳转：那条路径的落点 `_tts` 被
   *  `PreservePage.reproduce.test.tsx` 逐字钉住，是另一条闭环的语义。 */
  const goRecordLesson = async () => {
    setLessonBusy(true)
    setBookErr("")
    try {
      const rep = await p.reproduce()
      if (rep) navigate("/tts?tab=book")
    } finally {
      setLessonBusy(false)
    }
  }

  /** 「录课」关着时的一键开启（用户显式点击才会改配置，不静默动）。 */
  const enableBook = async () => {
    setEnablingBook(true)
    setBookErr("")
    try {
      await setPluginEnabled("sound.audiobook", true)
      reloadCatalog()
    } catch (e) {
      setBookErr(e instanceof Error ? e.message : String(e))
    } finally {
      setEnablingBook(false)
    }
  }

  /** 「导出评测实验」：把录音按评测引擎的目录约定落盘（产品 → 研究的桥）。
   *
   *  与研究的关系：档案里的录音是**采集结果**，评测实验目录是**分析输入**，
   *  两者之间原本靠手工拷文件 —— 而拷漏一档不报错（`analyze` 会报
   *  "数据不足以判定"，把错因指向统计），拷错档更不报错（自变量标签直接错）。
   *  这里让后端按结构拷、档位名现读。
   *
   *  导出成功后把路径显示出来 —— 用户要拿它去跑 measure/analyze。 */
  const [exportPath, setExportPath] = useState("")
  const doExport = async () => {
    setExportPath("")
    const res = await p.exportExperiment()
    if (res) setExportPath(res.root)
  }

  if (p.loading) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 正在读取声音档案…
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-4">
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-lg font-medium">
          <ShieldCheck className="h-5 w-5 text-primary" />
          声音保存 —— 给嗓子留一份「备份」
        </h1>
        <p className="text-sm text-muted-foreground">
          在嗓子还好的时候录一段您自己的声音，存在这台电脑里。以后打字，就能让这个声音替您讲课、读书 ——
          咽炎、声带小结的高发原因就是长期高强度用嗓，提前留一份是有意义的准备。
        </p>
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Info className="h-3.5 w-3.5 shrink-0" />
          <span>
            录音与生成结果<b>全部保存在本机</b>，不会上传到任何服务器；也只有您本人确认同意后，档案才能用来生成声音。
          </span>
          {p.storeDir ? (
            <span className="truncate font-mono text-[11px]" title={p.storeDir}>
              存放位置：{p.storeDir}
            </span>
          ) : null}
        </p>
      </header>

      {p.error ? <ErrorPanel title={p.error.title} detail={p.error.detail} /> : null}
      {p.notice ? (
        <div className="rounded-lg border border-primary/30 bg-primary/5 px-3 py-2 text-sm text-foreground">
          {p.notice}
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        {/* ---------- 左：档案列表 ---------- */}
        <aside className="space-y-3">
          <div className="rounded-xl border border-border bg-card p-3">
            <div className="mb-2 flex items-center gap-1.5 text-sm font-medium">
              <UserRoundPlus className="h-4 w-4" /> 新建档案
            </div>
            <div className="flex gap-2">
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void p.create(label)
                }}
                placeholder="给这份声音起个名字"
                className="min-w-0 flex-1 rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
              />
              <button
                onClick={() => void p.create(label)}
                disabled={p.busy === "create"}
                className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
              >
                {p.busy === "create" ? <Loader2 className="h-4 w-4 animate-spin" /> : "建立"}
              </button>
            </div>
          </div>

          <div className="space-y-1.5">
            {p.profiles.length === 0 ? (
              <p className="px-1 text-xs text-muted-foreground">还没有档案。先建一份，再按引导录音。</p>
            ) : null}
            {p.profiles.map((s) => (
              <button
                key={s.id}
                onClick={() => p.setSelectedId(s.id)}
                className={cn(
                  "flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors",
                  p.selectedId === s.id
                    ? "border-primary/50 bg-primary/5"
                    : "border-border bg-card hover:border-border/80"
                )}
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium">{s.label}</span>
                  <span className="block text-[11px] text-muted-foreground">
                    {s.consent_ok ? "已同意" : "未同意"} · {s.n_refs} 档参考音
                  </span>
                </span>
                {s.consent_ok && s.n_refs > 0 ? (
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
                ) : (
                  <CircleDashed className="h-4 w-4 shrink-0 text-muted-foreground" />
                )}
              </button>
            ))}
          </div>
        </aside>

        {/* ---------- 右：详情 ---------- */}
        <main className="space-y-3">
          {!p.profile ? (
            <div className="rounded-xl border border-dashed border-border p-10 text-center text-sm text-muted-foreground">
              从左边选一份档案，或先新建一份。
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <h2 className="text-base font-medium">{p.profile.label}</h2>
                <button
                  onClick={() => void p.remove(p.profile!.id)}
                  disabled={p.busy === "delete"}
                  className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-red-600"
                >
                  <Trash2 className="h-3.5 w-3.5" /> 删除档案
                </button>
              </div>

              {/* ① 知情同意 */}
              <Card title="知情同意" step={1} done={hasConsent}>
                {hasConsent ? (
                  <p className="text-xs text-muted-foreground">
                    已于 {p.profile.consent!.agreed_at} 记录（同意书版本 {p.profile.consent!.version}）。
                    这份记录会一直留在这个档案里。
                  </p>
                ) : (
                  <div className="space-y-2">
                    <p className="text-xs text-muted-foreground">
                      这一步只需要确认一件事：要保存的声音是<b>您本人</b>的（或您已获得授权）。
                      确认之后，这份档案才能用来生成声音 —— 这是对声音主人权利的保护。
                    </p>
                    <p className="rounded-md bg-muted/50 p-3 text-xs leading-relaxed">
                      {p.guide?.consent.statement}
                    </p>
                    <div className="flex gap-2">
                      <input
                        value={operator}
                        onChange={(e) => setOperator(e.target.value)}
                        placeholder="署名（可留空）"
                        className="min-w-0 flex-1 rounded-md border border-input bg-background px-2 py-1.5 text-sm outline-none focus:border-primary"
                      />
                      <button
                        onClick={() => void p.consent(operator)}
                        disabled={p.busy === "consent"}
                        className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
                      >
                        {p.busy === "consent" ? <Loader2 className="h-4 w-4 animate-spin" /> : "我确认"}
                      </button>
                    </div>
                    <p className="text-[11px] text-muted-foreground">
                      没有这一步，档案不能用于生成 —— 点上面的「我确认」即可，之后不会再打扰您。
                    </p>
                  </div>
                )}
              </Card>

              {/* ② 录音 */}
              <Card title="按引导录音" step={2} done={refs.length > 0}>
                <p className="mb-3 text-xs text-muted-foreground">
                  建议先录 <b>10 秒</b>档：日常讲课够用，一分钟内就能录完。想效果更好再录更长的档 ——
                  录得越久效果越稳，但收益会慢慢饱和。
                  {p.guide ? ` 完整脚本约 ${p.guide.script_capacity_s} 秒。` : ""}
                </p>
                {/* ★ 推荐路径：一次录满，后端按前缀切全部档 —— 这是评测协议要求的采集形态。
                    逐档录四次 = 四条不同 take，「时长」自变量会被"这次录得好不好"混淆。 */}
                <div className="mb-3 rounded-md border border-primary/30 bg-primary/5 p-3">
                  <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium">
                    <CheckCircle2 className="h-3.5 w-3.5 text-primary" />
                    推荐：一次录满，自动切出四档
                  </div>
                  <p className="mb-2 text-xs text-muted-foreground">
                    按脚本念到 <b>{longestTag}</b>（约 {longestSentences} 句），停止后系统自动按
                    前 3/10/30/60 秒切档。<b>四档来自同一条录音</b>，这才是研究要求的采集方式。
                  </p>
                  <button
                    onClick={() =>
                      p.recordingFor > 0 ? p.stopRecording() : void p.startFullRecording()
                    }
                    disabled={Boolean(p.busy) || p.recordingFollowup ||
                      (p.recordingFor > 0 && p.recordingFor !== longestS)}
                    className={cn(
                      "flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm disabled:opacity-40",
                      p.recordingFor === longestS
                        ? "border-red-500/50 bg-red-500/10"
                        : "border-primary/40 bg-card"
                    )}
                  >
                    {p.recordingFor === longestS ? (
                      <>
                        <Square className="h-3.5 w-3.5" /> 停止（已录 {p.recordSeconds}s）
                      </>
                    ) : (
                      <>
                        <Mic className="h-3.5 w-3.5" /> 开始录整段
                      </>
                    )}
                  </button>
                </div>
                <div className="flex flex-wrap gap-2">
                  {(p.guide?.durations ?? []).map((d) => {
                    const hint = TIER_HINTS[d.tag] ?? ""
                    return (
                      <button
                        key={d.tag}
                        onClick={() =>
                          p.recordingFor === d.duration_s
                            ? p.stopRecording()
                            : void p.startRecording(d.duration_s)
                        }
                        disabled={!d.covered || Boolean(p.busy) || p.recordingFollowup ||
                          (p.recordingFor > 0 && p.recordingFor !== d.duration_s)}
                        title={d.covered ? `照着脚本念前约 ${d.sentences} 句` : "参考脚本不够长，录不满这一档"}
                        className={cn(
                          "flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm disabled:opacity-40",
                          refTags.has(d.tag) ? "border-emerald-500/40 bg-emerald-500/10" : "border-border bg-card",
                          p.recordingFor === d.duration_s && "border-red-500/50 bg-red-500/10"
                        )}
                      >
                        {p.recordingFor === d.duration_s ? (
                          <>
                            <Square className="h-3.5 w-3.5" /> 停止（已录 {p.recordSeconds}s）
                          </>
                        ) : (
                          <>
                            <Mic className="h-3.5 w-3.5" />
                            {d.tag}
                            {hint ? <span className="text-[11px] opacity-70">{hint}</span> : null}
                            {refTags.has(d.tag) ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" /> : null}
                          </>
                        )}
                      </button>
                    )
                  })}
                  {p.busy === "upload" ? (
                    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> 正在保存并转成 wav…
                    </span>
                  ) : null}
                </div>

                {p.recordingFor > 0 ? (
                  <p className="mt-3 rounded-md bg-red-500/10 p-3 text-xs">
                    正在录 <b>{p.guide?.durations.find((d) => d.duration_s === p.recordingFor)?.tag}</b> 档，
                    请照着下面的脚本念，念够时长后点「停止」。已录 {p.recordSeconds}s。
                  </p>
                ) : null}

                {/* 录音时脚本自动展开 —— 老师不该在录音中途还要去找「展开脚本」按钮。
                    随访录音也一样：随访要念「同样的句子」才有可比性。 */}
                {showScript || p.recordingFor > 0 || p.recordingFollowup ? (
                  <ol className="mt-2 max-h-52 list-decimal space-y-1 overflow-auto rounded-md bg-muted/40 p-3 pl-8 text-xs leading-relaxed">
                    {(p.guide?.script ?? []).map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ol>
                ) : (
                  <button
                    onClick={() => setShowScript(true)}
                    className="mt-3 text-xs text-primary hover:underline"
                  >
                    展开录音脚本（照着念就行）
                  </button>
                )}

                {refs.length > 0 ? (
                  <ul className="mt-3 space-y-1 text-[11px] text-muted-foreground">
                    {refs.map((r) => (
                      <li key={r.tag}>
                        {r.tag} 档已保存
                        {r.actual_s != null ? `（实际 ${r.actual_s}s）` : ""}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </Card>

              {/* ③ 体检与复现 */}
              <Card title="体检与复现" step={3} done={p.gate?.verdict === "ready"}>
                {canReproduce && !p.gate ? (
                  <p className="mb-3 text-xs text-muted-foreground">
                    录音已经就绪 —— 现在就可以点 <b>去复现</b> 直接使用；体检是可选项，帮您确认录音质量。
                  </p>
                ) : null}
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => void p.runGate()}
                    disabled={p.busy === "gate"}
                    title="客观指标（HNR / 音高抖动 / 音强抖动）给录音质量一个参考分，不影响能否使用"
                    className="rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
                  >
                    {p.busy === "gate" ? (
                      <span className="flex items-center gap-1.5">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 正在体检…
                      </span>
                    ) : (
                      "给声音做个体检（可选）"
                    )}
                  </button>
                  {canReproduce ? (
                    <button
                      onClick={() => void goReproduce()}
                      disabled={Boolean(p.busy) || lessonBusy}
                      title="把最长档参考音注册成音色库音色并选中，然后跳到「输字变声」直接输字合成。重录过的话再点一次就会刷新。"
                      className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50"
                    >
                      {p.busy === "reproduce" && !lessonBusy ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <AudioLines className="h-3.5 w-3.5" />
                      )}
                      去复现 · 让它说话
                    </button>
                  ) : null}
                  {/* 老师的主线落点：保存声音的目的多半是「把一节课的讲稿念完」，
                      而不是合成一句话。有声书能力关着时给一键开启（用户显式点击）。 */}
                  {canReproduce ? (
                    bookOn ? (
                      <button
                        onClick={() => void goRecordLesson()}
                        disabled={Boolean(p.busy) || lessonBusy}
                        title="注册成音色库音色后跳到「录课」—— 粘贴一节课的讲稿，按句合成并自动拼接成一整段音频。"
                        className="flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-sm text-primary disabled:opacity-50"
                      >
                        {lessonBusy ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <BookOpen className="h-3.5 w-3.5" />
                        )}
                        去录一节课
                      </button>
                    ) : (
                      <button
                        onClick={() => void enableBook()}
                        disabled={enablingBook}
                        title="「录课」能力现在是关闭的。点一下打开，之后就能把一节课的讲稿一次跑完。"
                        className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
                      >
                        {enablingBook ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <BookOpen className="h-3.5 w-3.5" />
                        )}
                        开启「录课」
                      </button>
                    )
                  ) : null}
                  {/* 研究落点：把录音导出成评测引擎能直接读的实验目录。
                      老师/学生做「大创」研究时，这一步原本要手工拷文件 ——
                      而拷漏一档不报错（analyze 会报"数据不足以判定"，
                      把错因指向统计），拷错档更不报错（自变量标签直接错）。 */}
                  {canReproduce ? (
                    <button
                      onClick={() => void doExport()}
                      disabled={Boolean(p.busy) || lessonBusy}
                      title="把这份档案的四档参考音与真值录音，按评测引擎的目录约定导出到 outputs/voicebank/。导出后即可跑 measure / analyze。需要先确认知情同意。"
                      className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm disabled:opacity-50"
                    >
                      {p.busy === "export" ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <FileDown className="h-3.5 w-3.5" />
                      )}
                      导出评测实验
                    </button>
                  ) : null}
                  {p.gate ? (
                    <span
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
                        VERDICT_TEXT[p.gate.verdict]?.tone
                      )}
                    >
                      {p.gate.verdict === "ready" ? (
                        <CheckCircle2 className="h-3.5 w-3.5" />
                      ) : (
                        <CircleAlert className="h-3.5 w-3.5" />
                      )}
                      {VERDICT_TEXT[p.gate.verdict]?.label}
                    </span>
                  ) : null}
                </div>

                {bookErr ? (
                  <p className="mt-2 text-xs text-red-500">开启「录课」失败：{bookErr}</p>
                ) : null}

                {exportPath ? (
                  <p className="mt-2 break-all text-xs text-muted-foreground">
                    已导出到 <code className="rounded bg-muted px-1">{exportPath}</code>
                    ，可直接跑 <code className="rounded bg-muted px-1">measure</code> /{" "}
                    <code className="rounded bg-muted px-1">analyze</code>。
                  </p>
                ) : null}

                {p.gate ? (
                  <div className="mt-3 space-y-3">
                    <p className="text-xs text-muted-foreground">{VERDICT_TEXT[p.gate.verdict]?.hint}</p>
                    {p.gate.reasons.length > 0 ? (
                      <ul className="space-y-1 text-xs">
                        {p.gate.reasons.map((r, i) => (
                          <li key={i} className="flex gap-1.5">
                            <span className="text-amber-500">·</span>
                            <span>{r}</span>
                          </li>
                        ))}
                      </ul>
                    ) : null}

                    {p.gate.metrics ? (
                      <div className="space-y-1">
                        <p className="text-[11px] text-muted-foreground">客观指标（数值仅供参考，看三灯结论就行）：</p>
                        <div className="grid grid-cols-2 gap-1.5 text-[11px] sm:grid-cols-3">
                          {Object.entries(p.gate.metrics).map(([k, v]) => (
                            <div key={k} className="rounded-md bg-muted/40 px-2 py-1.5">
                              <span className="block text-muted-foreground">{k}</span>
                              <span className="font-mono">{v == null ? "—" : v}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : null}

                    {p.gate.lamps?.lamps?.length ? (
                      <div className="space-y-1">
                        {p.gate.lamps.lamps.map((l, i) => (
                          <div key={i} className="flex items-center gap-2 text-[11px]">
                            <span
                              className={cn(
                                "h-2 w-2 shrink-0 rounded-full",
                                l.lamp ? LAMP_DOT[l.lamp] : "bg-muted-foreground/40"
                              )}
                            />
                            <span className="w-16 shrink-0 text-muted-foreground">{l.label}</span>
                            <span className="truncate">{l.detail}</span>
                          </div>
                        ))}
                        <p className="pt-1 text-[11px] text-muted-foreground">
                          三灯仅供参考，不决定能否使用 —— 它的阈值是在另一批录音上标定的，
                          换人换麦就会偏。
                        </p>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </Card>

              {/* ⑤ 声音随访：一次保存是快照，随访是曲线 —— 隔段时间复录一次，
                  三灯指标连成趋势（纵向随访）。措辞红线：这是「发声状态趋势参考」，
                  不是医疗诊断 —— 不出现「诊断 / 健康监测 / 疗效」（vitest 守卫钉住）。 */}
              <Card title="声音随访">
                <p className="mb-2 text-xs text-muted-foreground">
                  隔一段时间（比如每周）录一小段，这里会把每次的指标连成趋势，
                  帮您观察这份声音随时间的变化。建议照着「按引导录音」里的脚本念 ——
                  念同样的句子，趋势才有可比性。
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => (p.recordingFollowup ? p.stopRecording() : void p.startFollowupRecording())}
                    disabled={Boolean(p.busy) || p.recordingFor > 0}
                    title="录约 20 秒：念与之前同样的引导脚本。后端会算指标并追加一条趋势点（之前的记录不会被覆盖）。"
                    className={cn(
                      "flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm disabled:opacity-40",
                      p.recordingFollowup ? "border-red-500/50 bg-red-500/10" : "border-border bg-card"
                    )}
                  >
                    {p.recordingFollowup ? (
                      <>
                        <Square className="h-3.5 w-3.5" /> 停止（已录 {p.recordSeconds}s）
                      </>
                    ) : (
                      <>
                        <Mic className="h-3.5 w-3.5" /> 录一次随访
                      </>
                    )}
                  </button>
                  {p.busy === "followup-upload" ? (
                    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" /> 正在保存并算指标…
                    </span>
                  ) : null}
                </div>

                {p.trend && p.trend.n > 0 ? (
                  <div className="mt-3 grid gap-2 sm:grid-cols-3">
                    <Sparkline title="相似度（与档案参考音）" unit="" points={p.trend.points} lampIdx={0} />
                    <Sparkline title="自然度（HNR）" unit=" dB" points={p.trend.points} lampIdx={1} />
                    <Sparkline title="发声平稳度（H1-H2）" unit=" dB" points={p.trend.points} lampIdx={2} />
                  </div>
                ) : (
                  <p className="mt-3 text-xs text-muted-foreground">
                    还没有随访记录 —— 录第一次后，这里会出现趋势小图。
                  </p>
                )}
                <p className="mt-2 text-[11px] text-muted-foreground">
                  {p.trend?.hint ?? "这些数值只是发声状态的趋势参考，请结合当时的用嗓情况理解。"}
                </p>
              </Card>

              {/* ④ 操作日志 */}
              {p.events.length > 0 ? (
                <details className="rounded-xl border border-border bg-card p-4">
                  <summary className="cursor-pointer text-sm font-medium">
                    操作记录（{p.events.length} 条）
                  </summary>
                  <ul className="mt-2 space-y-1 font-mono text-[11px] text-muted-foreground">
                    {p.events.map((e, i) => (
                      <li key={i}>
                        {e.at} · {e.kind}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </>
          )}
        </main>
      </div>
    </div>
  )
}
