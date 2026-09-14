import { ArrowRight, AudioLines, Headphones, HelpCircle, Keyboard, Loader2, Mic2, Minus, Plus, Radio, RefreshCw, ShieldCheck, Sparkles, Speech, Users, Volume2, Wrench } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router-dom"
import { mediaUrl, type MarketItem } from "@/api/client"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { EffectLadderCard } from "@/components/EffectLadderCard"
import { cn } from "@/lib/utils"
import { useAppStore } from "@/store/useAppStore"
import { useHomeDemo } from "@/pages/Home/useHomeDemo"

/**
 * 首页 · 先玩再定制。
 * 首屏给「立刻能玩」的两条路（预置音色即点即听 / 输字让它说），
 * 玩出兴趣后再引导「定制自己的音色」（采集 → 存音色 → 输字 → 训练 → 送进微信/游戏）。
 */

/** 缩略图分类色（与音色市场保持一致） */
const CATEGORY_TONE: Array<[RegExp, string, string]> = [
  [/卡通|角色/, "from-amber-500/25 to-amber-500/5", "ring-amber-500/30"],
  [/女声/, "from-pink-500/25 to-pink-500/5", "ring-pink-500/30"],
  [/男声/, "from-sky-500/25 to-sky-500/5", "ring-sky-500/30"],
]
const DEFAULT_TONE = ["from-violet-500/25 to-violet-500/5", "ring-violet-500/30"] as const

/** 首页试听卡：demo 直接播；无 demo 首次生成（下载模型 → 转换），ready 后直播 */
function HomeDemoCard({ item, previews, onTrigger, isPlayable }: {
  item: MarketItem
  previews: ReturnType<typeof useHomeDemo>["previews"]
  onTrigger: (item: MarketItem) => void
  isPlayable: (url?: string) => boolean
}) {
  const voiceId = item.prefs?.voice_id ?? item.voice_id ?? ""
  const prev = previews[voiceId]
  const demoUrl = isPlayable(item.demo) ? mediaUrl(item.demo as string) : null
  const readyUrl = prev?.status === "ready" ? mediaUrl(prev.url) : null
  const tone = CATEGORY_TONE.find(([re]) => re.test(item.category ?? "")) ?? DEFAULT_TONE
  const [grad, ring] = tone
  const Initial = (item.name || item.repo || "?").trim().charAt(0).toUpperCase()
  const href = readyUrl ?? demoUrl
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-border bg-card/85 p-3.5 shadow-md transition hover:-translate-y-0.5 hover:shadow-lg">
      <div className={cn("relative flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br text-lg font-bold text-foreground ring-1", grad, ring)}>
        {Initial}
        {item.image && <img src={mediaUrl(item.image)} alt={item.name} className="absolute inset-0 h-full w-full object-cover" loading="lazy" />}
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-semibold text-card-foreground" title={item.desc || item.name}>{item.name}</p>
        <p className="mt-0.5 text-[10px] text-muted-foreground">{item.category ?? "音色"} · {item.platform}</p>
        {href ? (
          <StudioAudioPlayer src={href} label="试听" className="mt-1.5 min-w-0" />
        ) : prev?.status === "generating" ? (
          <span className="mt-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground" title="首次试听需先下载模型并转换，需几分钟">
            <Loader2 className="h-3 w-3 animate-spin text-primary" />试听准备中…
          </span>
        ) : prev?.status === "failed" || prev?.status === "skipped" ? (
          <button type="button" onClick={() => onTrigger(item)} title={prev.error || "重试生成试听"}
            className="mt-1.5 inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[11px] text-foreground transition hover:border-primary hover:text-primary">
            <RefreshCw className="h-3 w-3" />重试试听
          </button>
        ) : (
          <button type="button" onClick={() => onTrigger(item)}
            title={`点一下即听${item.size_hint_mb && !demoUrl ? `（首次需下载模型约 ${item.size_hint_mb}M）` : ""}`}
            className={cn("mt-1.5 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11px]",
              demoUrl ? "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20"
                : "border-border text-foreground hover:border-primary hover:text-primary")}>
            <Headphones className="h-3 w-3" />{demoUrl ? "播放试听" : "点一下即听"}
          </button>
        )}
      </div>
    </div>
  )
}

/** 玩出兴趣后，进阶三张卡（理念：先玩，再定制） */
const ADVANCED_ITEMS = [
  {
    to: "/workshop",
    icon: Mic2,
    title: "训练变声：专属嗓子",
    desc: "从你的素材开始，练一副只属于你的音色——不像预置，是「你那副」。",
    tag: "最像 · 慢工细活",
    tone: "default" as const,
  },
  {
    to: "/live?tab=rvc",
    icon: Radio,
    title: "实时变声：开麦就用",
    desc: "训练完成后，你说话、它出声，实时开麦直接用（游戏 / 会议 / 语音）。",
    tag: "要先用上面练出模型",
    tone: "default" as const,
  },
  {
    to: "/offlinevc",
    icon: Wrench,
    title: "工具箱：整段变声",
    desc: "把录好的整段音频一次性变成目标音色，适合配音与剪辑后期。",
    tag: "进阶",
    tone: "default" as const,
  },
]

/** 「做一副专属嗓子」定制闭环：5 步，每步讲清"做什么 + 为什么" */
const STEPS = [
  {
    icon: Volume2,
    title: "找 10 秒干净人声",
    todo: "桌宠右键一键内录（刷视频时顺手抓）、拖视频进来，或直接用麦克风录自己。",
    why: "人声越纯、背景乐越少，后面的变声越不像'机器人'。",
    to: "/workshop",
    cta: "去音色工坊",
  },
  {
    icon: ShieldCheck,
    title: "存成「我的音色」",
    todo: "系统自动去伴奏、切片段、挑出主说话人，你只管听和勾选保存。",
    why: "有了这个参考档案，它就认识这个声音了——后面所有变声都围着它转。",
    to: "/voices?tab=mine",
    cta: "去我的音色",
  },
  {
    icon: Speech,
    title: "输字变声，秒出",
    todo: "免训练路线：选刚建好的音色，输几个字就能先听到它的声音。",
    why: "10 秒就能验证「像不像」——这段素材值不值得留，一听便知。",
    to: "/tts?tab=single",
    cta: "去输字变声",
  },
  {
    icon: Radio,
    title: "想实时更像？攒到 1 分钟",
    todo: "把素材攒到约 1 分钟，一键生成训练语料，让系统照这个声音练一副专属嗓子。",
    why: "训练 = 按你的腔调量身打造，高音低音停顿语气都更贴，之后开麦就能用。",
    to: "/live?tab=rvc",
    cta: "去实时变声",
  },
  {
    icon: AudioLines,
    title: "送进微信 / 游戏",
    todo: "实时变声开麦直接在微信、游戏里生效；也可以把合成好的语音一键发到微信。",
    why: "终于走到头了：你的声音在真场景里替你说出来。",
    to: "/tts?tab=wechat",
    cta: "去微信发送",
  },
]

/** 术语科普：把进阶名词翻译成大白话（点击展开） */
const GLOSSARY = [
  {
    term: "训练（RVC 模型）",
    plain: "免训练像是'临时借了个相似的声音'；训练是'照你的口型、腔调、情绪量身练一副嗓子'——所以说语气、高低、停顿都更像你自己，还能开着麦实时变声。",
  },
  {
    term: "伴奏分离（demucs）",
    plain: "自动把背景音乐和说话声拆开，只留干净的说话声，免得配乐混进你的素材。",
  },
  {
    term: "说话人分离",
    plain: "视频里好几个人在说话？系统自动标出谁是谁，优先挑主角的声音，别把别人的话混进你的素材里。",
  },
  {
    term: "质检 A / B / C / D",
    plain: "像买菜挑水果：A、B 等级干净直接要，C 谨慎，D 别用。“响度达标”只代表音量合适，纯不纯以听感为准。",
  },
]

export function HomePage() {
  const voices = useAppStore((s) => s.voices)
  const [glossaryOpen, setGlossaryOpen] = useState(false)
  const demo = useHomeDemo()
  const featured = demo.items?.slice(0, 6) ?? []

  // 根据全局音色数据算"效果阶梯当前位置"（零后端改动，App 已 5s 轮询填充 voices）
  const hasRef = voices.some((v) => v.has_reference !== false && (v.has_reference === true || v.duration_s >= 3))
  const modelReady = voices.some((v) => v.model_ready === true)
  const longDataset = voices.some((v) => (v.dataset_count ?? 0) > 0)
  const totalSeconds = voices.reduce((sum, v) => sum + (v.duration_s || 0), 0)
  const currentLevel = modelReady ? 3 : longDataset ? 2 : hasRef ? 1 : 0

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      {/* Hero：先玩——零门槛第一条"立刻能玩"的路 */}
      <header className="relative overflow-hidden border-b border-border px-5 py-12 sm:px-8 lg:px-12 lg:py-16">
        <div className="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary/10 via-background to-card" aria-hidden="true" />
        <div className="relative mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">HOME / 先玩，再定制</p>
          <h2 className="mt-4 max-w-3xl font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            让任何声音，替你说
          </h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            先挑一个现成音色，免费试听、输字就能让它说话——不找素材、不训练，30
            秒先玩起来。玩顺了，再往下滑，把它练成你的专属嗓子。
          </p>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-12 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {/* 立刻能玩：首屏第一件事 = 预置音色即点即听 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">STEP 0 · 不花时间</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">先玩起来：点一个音色，马上听到它</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">下面全是官方预置的开源音色——不用找素材、不用训练，点一下就能听。</p>
          </div>

          {demo.items === null ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, i) => (
                <div key={i} className="h-[92px] animate-pulse rounded-2xl border border-border bg-card/60" />
              ))}
            </div>
          ) : featured.length === 0 ? (
            <div className="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-border bg-card/50 px-6 py-10 text-center">
              <Users className="h-6 w-6 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">预置清单还没连上后端，稍后自动刷新；也可以先去音色市场逛逛。</p>
              <Link to="/voices?tab=market" className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20">
                去音色市场 <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {featured.map((item) => (
                <HomeDemoCard
                  key={item.id ?? item.name}
                  item={item}
                  previews={demo.previews}
                  onTrigger={demo.triggerPreview}
                  isPlayable={demo.isPlayable}
                />
              ))}
            </div>
          )}

          <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-primary" />
              预置的都是开源音色，免费、本地运行，不涉及版权问题。
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Link to="/tts?tab=single"
                className="inline-flex items-center gap-1.5 rounded-md border border-primary/50 bg-primary px-3 py-2 text-xs font-medium text-primary-foreground shadow-md transition hover:bg-primary/90">
                <Keyboard className="h-3.5 w-3.5" />挑好了？输字让它说话
              </Link>
              <Link to="/voices?tab=market"
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary">
                更多音色 <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          </div>
        </section>

        {/* 玩出兴趣后，再谈定制 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">WANT MORE</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">玩过了？三条路把你带向"专属"</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">想要"不撞声音"、开麦就能用的专属嗓子？往下是正经玩法，随时可以回来。</p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {ADVANCED_ITEMS.map(({ to, icon: Icon, title, desc, tag, tone }) => (
              <Link
                key={title}
                to={to}
                className="group rounded-2xl border border-border bg-card/85 p-5 shadow-md transition hover:-translate-y-0.5 hover:shadow-lg"
              >
                <span className={`flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-background ${tone === "default" ? "text-muted-foreground" : "text-primary"}`}>
                  <Icon className="h-5 w-5" />
                </span>
                <h4 className="mt-3 text-sm font-semibold text-card-foreground">{title}</h4>
                <p className="mt-1 inline-block rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{tag}</p>
                <p className="mt-2.5 text-xs leading-5 text-muted-foreground">{desc}</p>
                <span className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-primary opacity-0 transition group-hover:opacity-100">
                  去看看 <ArrowRight className="h-3.5 w-3.5" />
                </span>
              </Link>
            ))}
          </div>
        </section>

        {/* 定制闭环：给想要专属音色的人 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">FIRST VOICE</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">想做专属嗓子？五步走完</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">下面是完整教程。每一屏顶部都有导览小助手，不懂的词随时点开下面的白话解释。</p>
          </div>
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-md sm:p-6">
            <ol className="flex flex-col gap-1">
              {STEPS.map(({ icon: Icon, title, todo, why, to, cta }, i) => (
                <li key={title} className="group flex gap-4">
                  <div className="flex flex-col items-center self-stretch pt-1">
                    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-primary/40 bg-primary/10 text-primary">
                      <Icon className="h-4 w-4" />
                    </span>
                    {i < STEPS.length - 1 && <span className="mt-1 w-px flex-1 bg-border" aria-hidden="true" />}
                  </div>
                  <div className={`min-w-0 flex-1 pb-6 ${i === STEPS.length - 1 ? "pb-0" : ""}`}>
                    <p className="text-sm font-semibold text-card-foreground">
                      <span className="mr-1.5 font-mono text-[10px] text-muted-foreground">{String(i + 1).padStart(2, "0")}</span>
                      {title}
                    </p>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">{todo}</p>
                    <p className="mt-1.5 flex items-start gap-1.5 text-[11px] leading-4 text-primary/80">
                      <Sparkles className="mt-0.5 h-3 w-3 shrink-0" />为什么：{why}
                    </p>
                    <Link to={to} className="mt-3 inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20">
                      {cta} <ArrowRight className="h-3.5 w-3.5" />
                    </Link>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* 效果阶梯：为什么要继续采集 / 训练 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">WHY CONTINUE</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">为什么还要继续攒素材？</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">这条阶梯就是答案：同样一段字，素材越足，越像是"那个人"亲口说的。</p>
          </div>
          <EffectLadderCard currentLevel={currentLevel} collectedSeconds={totalSeconds} modelReady={modelReady} />
        </section>

        {/* 术语科普：进阶名词大白话 */}
        <section>
          <div className="mb-5 flex items-center justify-between">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">GLOSSARY</p>
              <h3 className="mt-2 text-2xl font-semibold text-foreground">听不懂的词，点开看白话</h3>
            </div>
            <button
              type="button"
              onClick={() => setGlossaryOpen((o) => !o)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              {glossaryOpen ? <Minus className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
              {glossaryOpen ? "收起" : "展开"}
            </button>
          </div>
          {glossaryOpen && (
            <div className="grid gap-3 md:grid-cols-2">
              {GLOSSARY.map(({ term, plain }) => (
                <div key={term} className="rounded-2xl border border-border bg-card/85 p-4 shadow-md">
                  <p className="flex items-center gap-2 text-sm font-medium text-card-foreground">
                    <HelpCircle className="h-4 w-4 text-primary" />{term}
                  </p>
                  <p className="mt-2 text-xs leading-5 text-muted-foreground">{plain}</p>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* 底线：全程本地 + 版权红线 */}
        <section className="flex flex-col gap-3 rounded-2xl border border-border bg-card/85 p-5 text-xs leading-5 text-muted-foreground shadow-md sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 shrink-0 text-primary" />
            <span>全程本地处理，不上传任何云端。只克隆自己或已授权的声音。</span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Users className="h-4 w-4 text-primary" />
            <Link to="/voices?tab=market" className="font-medium text-primary transition hover:opacity-80">也可以试试预置的开源音色</Link>
          </div>
        </section>
      </main>
    </div>
  )
}