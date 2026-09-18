import { ArrowRight, AudioLines, Check, CircleAlert, Headphones, HelpCircle, Loader2, MapPin, Mic2, Minus, Plus, Radio, RefreshCw, ShieldCheck, Sparkles, Speech, Star, Users, Volume2, Wrench } from "lucide-react"
import { useCallback, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { mediaUrl, type MarketItem } from "@/api/client"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { EffectLadderCard } from "@/components/EffectLadderCard"
import { Card, PageShell, Section } from "@/components/layout/PageShell"
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

/** 首页试听卡：demo 直接播；无 demo 首次生成（下载模型 → 转换），ready 后直播。
 *  主动作「用它开麦说话」：已装直接跳实时变声并预选；未装一键安装→装完自动跳。 */
function HomeDemoCard(props: {
  item: MarketItem
  previews: ReturnType<typeof useHomeDemo>["previews"]
  onTrigger: (item: MarketItem) => void
  isPlayable: (url?: string) => boolean
  installed: string[]
  installing: ReturnType<typeof useHomeDemo>["installing"]
  onUseIt: (item: MarketItem) => void
  fav: boolean
  onToggleFav: () => void
  compareOn: boolean
  picked: boolean
  onTogglePick: () => void
}) {
  const { item, previews, onTrigger, isPlayable, installed, installing, onUseIt, fav, onToggleFav, compareOn, picked, onTogglePick } = props
  const voiceId = item.prefs?.voice_id ?? item.voice_id ?? ""
  const prev = previews[voiceId]
  const demoUrl = isPlayable(item.demo) ? mediaUrl(item.demo as string) : null
  const readyUrl = prev?.status === "ready" ? mediaUrl(prev.url) : null
  const tone = CATEGORY_TONE.find(([re]) => re.test(item.category ?? "")) ?? DEFAULT_TONE
  const [grad, ring] = tone
  const Initial = (item.name || item.repo || "?").trim().charAt(0).toUpperCase()
  const href = readyUrl ?? demoUrl
  const isInstalled = installed.includes(voiceId)
  const busyInstall = installing?.voiceId === voiceId
  const failedInstall = installing?.voiceId === voiceId && (installing.phase === "安装失败" || installing.phase === "启动失败")
  return (
    <div className="relative flex items-center gap-3 rounded-2xl border border-border bg-card/85 p-3.5 shadow-md transition hover:-translate-y-0.5 hover:shadow-lg">
      <button
        type="button"
        onClick={onToggleFav}
        aria-label={fav ? `取消收藏 ${item.name}` : `收藏 ${item.name}`}
        title={fav ? "取消收藏" : "收藏备用"}
        className={cn(
          "absolute right-2 top-2 z-10 flex h-6 w-6 items-center justify-center rounded-full transition",
          fav ? "text-amber-400" : "text-muted-foreground/60 hover:text-amber-400",
        )}
      >
        <Star className={cn("h-3.5 w-3.5", fav && "fill-current")} />
      </button>
      <div className={cn("relative flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br text-lg font-bold text-foreground ring-1", grad, ring)}>
        {Initial}
        {item.image && <img src={mediaUrl(item.image)} alt={item.name} className="absolute inset-0 h-full w-full object-cover" loading="lazy" />}
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-semibold text-card-foreground" title={item.desc || item.name}>{item.name}</p>
        <p className="mt-0.5 text-[10px] text-muted-foreground">
          {item.category ?? "音色"} · {item.platform}
          <span className={cn("ml-1.5 rounded-full px-1.5 py-px text-[9px]",
            isInstalled ? "bg-emerald-500/15 text-emerald-500" : "bg-muted text-muted-foreground")}>
            {isInstalled ? "已安装" : busyInstall ? "安装中" : "未安装"}
          </span>
        </p>
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
        <div className="mt-1.5">
          {compareOn ? (
            <button type="button" onClick={onTogglePick}
              className={cn("inline-flex w-full items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium transition",
                picked
                  ? "border border-primary bg-primary text-primary-foreground"
                  : "border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20")}>
              <Check className="h-3 w-3" />{picked ? "已选，和另一边一起听" : "加入对比"}
            </button>
          ) : busyInstall ? (
            <span className="inline-flex w-full items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 text-[11px] text-primary" title={installing?.error || "正在安装，装完自动跳转实时变声"}>
              <Loader2 className="h-3 w-3 shrink-0 animate-spin" />{installing?.phase ?? "安装中…"}{installing && installing.percent > 0 ? ` ${installing.percent}%` : ""}
            </span>
          ) : failedInstall ? (
            <span className="inline-flex w-full items-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive" title={installing?.error || "安装失败"}>
              <CircleAlert className="h-3 w-3 shrink-0" />安装失败，点上方试听可重试
            </span>
          ) : (
            <button type="button" onClick={() => onUseIt(item)}
              className={cn("inline-flex w-full items-center justify-center gap-1.5 rounded-md px-2.5 py-1.5 text-[11px] font-medium transition",
                isInstalled
                  ? "border border-primary/50 bg-primary text-primary-foreground hover:bg-primary/90"
                  : "border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20")}>
              <Mic2 className="h-3 w-3" />{isInstalled ? "用它开麦说话" : `装好用它开麦${item.size_hint_mb ? `（约 ${item.size_hint_mb}M）` : ""}`}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Hero 里的声音可视化。
 * 纯 CSS 竖条（高度按钟形包络给定，逐条错开相位），呼应"变声"这件事本身。
 * 刻意不用图片/视频：零网络请求、零版权负担，且自动跟随明暗主题。
 */
const WAVE_BARS = [16, 28, 20, 38, 30, 52, 40, 66, 48, 82, 58, 96, 72, 100, 84, 92, 62, 86, 50, 70, 38, 56, 30, 44, 24, 34, 18, 26]

function WaveBars() {
  return (
    <div className="flex h-24 items-center justify-center gap-1 lg:h-48" aria-hidden="true">
      {WAVE_BARS.map((height, i) => (
        <span
          key={i}
          className="w-1.5 rounded-full bg-gradient-to-t from-primary/20 via-primary/70 to-primary"
          style={{
            height: `${height}%`,
            animation: `eq ${1.1 + (i % 6) * 0.17}s ease-in-out ${(i % 9) * 0.07}s infinite`,
          }}
        />
      ))}
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
  },
  {
    to: "/live?tab=rvc",
    icon: Radio,
    title: "实时变声：开麦就用",
    desc: "你说话、它出声，游戏 / 会议 / 语音一个键直接用。预置音色装好即可开麦；想要专属嗓子，就按上面的路线练一副。",
    tag: "装好即开麦",
  },
  {
    to: "/offlinevc",
    icon: Wrench,
    title: "工具箱：整段变声",
    desc: "把录好的整段音频一次性变成目标音色，适合配音与剪辑后期。",
    tag: "进阶",
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
  {
    term: "零炼丹",
    plain: "炼丹是圈内对'训练模型'的戏称，听起来要懂参数、会配环境。这里指的是：你只要丢一段素材进来，去伴奏、切片、打分、剔除脏片段、挑主说话人全是自动的——你唯一要做的是听一遍并点保存。",
  },
]

export function HomePage() {
  const voices = useAppStore((s) => s.voices)
  const [glossaryOpen, setGlossaryOpen] = useState(false)
  const demo = useHomeDemo()
  const navigate = useNavigate()
  const featured = demo.items?.slice(0, 6) ?? []
  // 收藏/对比：帮挑音色的小工具，状态只在首页内
  const [favOnly, setFavOnly] = useState(false)
  const [compareOn, setCompareOn] = useState(false)
  const [comparePicks, setComparePicks] = useState<string[]>([])
  const vidOf = (item: MarketItem) => item.prefs?.voice_id ?? item.voice_id ?? ""
  const shownItems = favOnly ? featured.filter((it) => demo.favs.includes(vidOf(it))) : featured
  const togglePick = (vid: string) =>
    setComparePicks((prev) =>
      prev.includes(vid) ? prev.filter((x) => x !== vid) : prev.length >= 2 ? [...prev.slice(1), vid] : [...prev, vid],
    )
  const pickedItems = comparePicks
    .map((vid) => featured.find((it) => vidOf(it) === vid))
    .filter((it): it is MarketItem => Boolean(it))

  /** 「用它开麦说话」：已装直接跳实时变声并预选；未装先一键安装，装完自动跳 */
  const handleUseIt = useCallback(
    async (item: MarketItem) => {
      const voiceId = item.prefs?.voice_id ?? item.voice_id ?? ""
      const ok = await demo.installVoice(item) // 已装时内部直接返回 true
      if (ok && voiceId) navigate(`/live?tab=rvc&voice=${encodeURIComponent(voiceId)}`)
    },
    [demo, navigate],
  )

  // 根据全局音色数据算"效果阶梯当前位置"（零后端改动，App 已 5s 轮询填充 voices）
  const hasRef = voices.some((v) => v.has_reference !== false && (v.has_reference === true || v.duration_s >= 3))
  const modelReady = voices.some((v) => v.model_ready === true)
  const longDataset = voices.some((v) => (v.dataset_count ?? 0) > 0)
  const totalSeconds = voices.reduce((sum, v) => sum + (v.duration_s || 0), 0)
  const currentLevel = modelReady ? 3 : longDataset ? 2 : hasRef ? 1 : 0

  // 「你现在走到哪一步」：五步教程的卡点提示（与阶梯同源，纯前端判断）
  const stepTip = modelReady
    ? { text: "专属模型已就绪！最后一步：送进微信 / 游戏里实际用起来。", to: "/tts?tab=wechat", cta: "去微信发送" }
    : longDataset
      ? { text: `语料已攒好${totalSeconds >= 60 ? `（共 ${Math.round(totalSeconds)} 秒）` : ""}，下一步训练出模型就能实时开麦。`, to: "/live?tab=rvc", cta: "去实时变声训练" }
      : hasRef
        ? { text: "已有参考声音：先输几个字听听像不像；想要实时开麦，再按第 4 步攒到约 1 分钟。", to: "/tts?tab=single", cta: "去输字变声" }
        : voices.length === 0
          ? { text: "你还没有任何音色档案，五步就从第 1 步「找 10 秒干净人声」开始。", to: "/workshop", cta: "去音色工坊" }
          : { text: "还没有参考声音，回到第 1 步采集一段干净人声，再存成「我的音色」。", to: "/workshop", cta: "去音色工坊" }

  return (
    <div className="min-h-full">
      {/* Hero —— 全页唯一的大标题。
          此前顶栏写着「首页」、页内 hero 又贴一个 `HOME / 先玩，再定制`、往下还有七个
          等重的节标题，同一个词被砸了三次。这里收敛成一处，并把「先听一个」提到首屏。 */}
      <section className="hero-canvas relative overflow-hidden border-b border-border">
        <PageShell padded={false} className="relative py-10 lg:py-12">
          {/* grid-cols-1 不能省：不写的话窄屏是 auto 轨道，会被 h1 的 max-w-3xl 撑到 768px
              并把整页顶出视口（中文长句的 max-content 很宽，这正是窄屏横向溢出的来源）。 */}
          <div className="grid grid-cols-1 items-center gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,400px)] lg:gap-12">
            <div className="min-w-0">
              <h1 className="max-w-3xl font-display text-3xl font-bold tracking-tight text-foreground sm:text-4xl lg:text-5xl">
                让任何声音，<span className="text-gradient">替你说</span>
              </h1>
              <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
                先挑一个现成音色，点一下就能听。听中意了，装上开麦就能直接替你说——不找素材、不训练，先玩起来。玩顺了，再往下滑，把它练成你的专属嗓子。
              </p>
              <div className="mt-6 flex flex-wrap items-center gap-3">
                <a href="#play" className="btn-primary">
                  <Headphones className="h-4 w-4" />
                  先听一个
                </a>
                <Link to="/workshop" className="btn-ghost">
                  <Mic2 className="h-4 w-4" />
                  我想做自己的
                </Link>
              </div>
            </div>
            <WaveBars />
          </div>
        </PageShell>
      </section>

      <PageShell className="pt-10">
        <div className="space-y-14">
        {/* 立刻能玩：首屏第一件事 = 预置音色即点即听 */}
        <Section
          id="play"
          eyebrow="第 0 步 · 不花时间"
          title="点一个音色，马上听到它"
          desc="下面全是官方预置的开源音色——不用找素材、不用训练，点一下就能听。"
          actions={
            <>
              <button
                type="button"
                onClick={() => setFavOnly((v) => !v)}
                className={cn("inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition",
                  favOnly
                    ? "border-amber-400/50 bg-amber-400/10 text-amber-400"
                    : "border-border bg-card text-muted-foreground hover:border-amber-400/50 hover:text-amber-400")}
                title="只显示你点过收藏的心形音色"
              >
                <Star className={cn("h-3.5 w-3.5", favOnly && "fill-current")} />
                只看收藏{demo.favs.length > 0 ? `（${demo.favs.length}）` : ""}
              </button>
              <button
                type="button"
                onClick={() => setCompareOn((v) => !v)}
                className={cn("inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition",
                  compareOn
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border bg-card text-muted-foreground hover:border-primary hover:text-primary")}
                title="盲听对比：选两个音色，并排一起听再决定"
              >
                <AudioLines className="h-3.5 w-3.5" />
                {compareOn ? "退出对比" : "对比两个音色"}
              </button>
            </>
          }
        >

          {demo.items === null ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, i) => (
                <div key={i} className="h-[132px] animate-pulse rounded-2xl border border-border bg-card/60" />
              ))}
            </div>
          ) : shownItems.length === 0 ? (
            /* 两种"空"含义不同：此前共用一句"收藏夹还是空的"，
               而后端没连上、音色列表读不出来时也会走到这里，那句话是错的。 */
            favOnly ? (
              <Card tone="flat" className="flex flex-col items-center gap-3 px-6 py-8 text-center">
                <Star className="h-5 w-5 text-muted-foreground" />
                <p className="text-sm text-muted-foreground">收藏夹还是空的——点音色卡右上角的心形，把喜欢的先收起来再慢慢挑。</p>
                <button type="button" onClick={() => setFavOnly(false)}
                  className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20">
                  看全部音色 <ArrowRight className="h-3.5 w-3.5" />
                </button>
              </Card>
            ) : (
              <Card tone="flat" className="flex flex-col items-center gap-3 px-6 py-8 text-center">
                <Loader2 className="h-5 w-5 animate-spin text-primary" />
                <p className="max-w-md text-sm leading-6 text-muted-foreground">
                  音色列表还没读出来。本地服务刚启动要等一会儿（首次会加载模型）；一直没动静就点右上角的状态胶囊做一次环境体检。
                </p>
                <Link to="/voices?tab=market"
                  className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20">
                  去音色市场看看 <ArrowRight className="h-3.5 w-3.5" />
                </Link>
              </Card>
            )
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {shownItems.map((item) => (
                <HomeDemoCard
                  key={item.id ?? item.name}
                  item={item}
                  previews={demo.previews}
                  onTrigger={demo.triggerPreview}
                  isPlayable={demo.isPlayable}
                  installed={demo.installed}
                  installing={demo.installing}
                  onUseIt={handleUseIt}
                  fav={demo.favs.includes(vidOf(item))}
                  onToggleFav={() => demo.toggleFav(vidOf(item))}
                  compareOn={compareOn}
                  picked={comparePicks.includes(vidOf(item))}
                  onTogglePick={() => togglePick(vidOf(item))}
                />
              ))}
            </div>
          )}

          {compareOn && pickedItems.length > 0 && (
            <div className="mt-4 overflow-hidden rounded-2xl border border-primary/40 bg-card/90 shadow-lg backdrop-blur-xl">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
                <p className="flex items-center gap-2 text-xs font-medium text-card-foreground">
                  <AudioLines className="h-3.5 w-3.5 text-primary" />
                  盲听对比：两个音色说同一句话，点播放慢慢比较
                </p>
                <button type="button" onClick={() => setComparePicks([])}
                  className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary">
                  清空重选
                </button>
              </div>
              <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-2">
                {pickedItems.map((it) => {
                  const v = vidOf(it)
                  const pr = demo.previews[v]
                  const src = pr?.status === "ready" ? mediaUrl(pr.url) : demo.isPlayable(it.demo) ? mediaUrl(it.demo as string) : null
                  return (
                    <div key={v} className="flex flex-col gap-2 rounded-xl border border-border bg-background/60 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <p className="truncate text-xs font-semibold text-card-foreground">
                          {it.name}
                          <span className="ml-1.5 font-normal text-muted-foreground">{it.category ?? ""}</span>
                        </p>
                        <button type="button" title="移除"
                          onClick={() => togglePick(v)}
                          className="text-muted-foreground/60 transition hover:text-destructive" aria-label={`移除 ${it.name} 对比`}>
                          <Minus className="h-3.5 w-3.5" />
                        </button>
                      </div>
                      {src ? (
                        <StudioAudioPlayer src={src} label="对比试听" className="min-w-0" />
                      ) : pr?.status === "generating" ? (
                        <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                          <Loader2 className="h-3 w-3 animate-spin text-primary" />试听准备中…
                        </span>
                      ) : (
                        <button type="button" onClick={() => demo.triggerPreview(it)}
                          className="inline-flex items-center gap-1.5 self-start rounded-md border border-border px-2.5 py-1 text-[11px] text-foreground transition hover:border-primary hover:text-primary">
                          <Headphones className="h-3 w-3" />生成这版的试听
                        </button>
                      )}
                    </div>
                  )
                })}
                {pickedItems.length < 2 && (
                  <div className="flex items-center justify-center rounded-xl border border-dashed border-border bg-background/40 p-3 text-[11px] text-muted-foreground">
                    再选一个，并排听更直观
                  </div>
                )}
              </div>
            </div>
          )}

          <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="flex items-center gap-2 text-xs text-muted-foreground">
              <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-primary" />
              预置的都是开源音色，免费、本地运行，不涉及版权问题。
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Link to="/live?tab=rvc"
                className="inline-flex items-center gap-1.5 rounded-md border border-primary/50 bg-primary px-3 py-2 text-xs font-medium text-primary-foreground shadow-md transition hover:bg-primary/90">
                <Radio className="h-3.5 w-3.5" />挑好了？开麦说话
              </Link>
              <Link to="/voices?tab=market"
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary">
                更多音色 <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          </div>
        </Section>

        {/* 零炼丹信任条：打消"要自己炼模型"的顾虑。
            这块刻意放在 WANT MORE 之前 —— 用户看到"训练"两个字的第一反应是
            "我不会炼丹/要配环境/要洗数据"，不先破这个，下面三条路他根本不会点。 */}
        <Card as="section" tone="accent" className="p-5 sm:p-6">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-primary/30 bg-background text-primary">
              <Sparkles className="h-5 w-5" />
            </span>
            <div className="min-w-0 flex-1">
              <h3 className="text-base font-semibold text-foreground">零炼丹：你只管丢素材，剩下的我们挑</h3>
              <p className="mt-1.5 text-sm leading-6 text-muted-foreground">
                不用标注、不用洗数据、不用懂参数。系统会自动去伴奏、切片，
                再给每一条按<span className="text-card-foreground">时长 / 响度 / 削波 / 底噪 / 信噪比 / 说话人一致性</span>打 A–D 分，
                把混进来的他人声和音乐残响挑出去，只把干净的留下来做音色。
              </p>
              <ul className="mt-3 flex flex-wrap gap-2 text-[11px]">
                {["不用标注文字", "不用手动切片段", "不用逐条试听", "自动挑主说话人", "自动剔除脏片段"].map((t) => (
                  <li key={t} className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-2.5 py-1 text-muted-foreground">
                    <Check className="h-3 w-3 text-primary" />
                    {t}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </Card>

        {/* 玩出兴趣后，再谈定制 */}
        <Section
          eyebrow="想要更多"
          title="玩过了？三条路把你带向「专属」"
          desc="想要不撞声音、开麦就能用的专属嗓子？往下是正经玩法，随时可以回来。"
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            {ADVANCED_ITEMS.map(({ to, icon: Icon, title, desc, tag }) => (
              <Link
                key={title}
                to={to}
                className="group rounded-2xl border border-border bg-card/85 p-5 shadow-md transition hover:-translate-y-0.5 hover:shadow-lg"
              >
                <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-primary/25 bg-primary/10 text-primary transition group-hover:border-primary/45">
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
        </Section>

        {/* 定制闭环：给想要专属音色的人 */}
        <Section
          eyebrow="第一条音色"
          title="想做专属嗓子？五步走完"
          desc="下面是完整教程。页面边角有导览小助手，不懂的词随时到最下面看白话解释。"
        >
          {stepTip && (
            <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl border border-primary/30 bg-primary/10 px-4 py-3 text-xs leading-5 text-card-foreground">
              <MapPin className="h-4 w-4 shrink-0 text-primary" />
              <span className="min-w-0 flex-1">
                <span className="mr-1.5 font-semibold text-primary">你现在走到：</span>
                {stepTip.text}
              </span>
              <Link to={stepTip.to} className="inline-flex shrink-0 items-center gap-1 font-medium text-primary transition hover:opacity-80">
                {stepTip.cta} <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          )}
          <Card className="p-5 sm:p-6">
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
          </Card>
        </Section>

        {/* 效果阶梯：为什么要继续采集 / 训练 */}
        <Section
          eyebrow="为什么继续"
          title="为什么还要继续攒素材？"
          desc="这条阶梯就是答案：同样一段字，素材越足，越像是「那个人」亲口说的。"
        >
          <EffectLadderCard currentLevel={currentLevel} collectedSeconds={totalSeconds} modelReady={modelReady} />
        </Section>

        {/* 术语科普：进阶名词大白话（默认收起，需要时再展开） */}
        <Section
          eyebrow="术语表"
          title="听不懂的词，点开看白话"
          actions={
            <button
              type="button"
              onClick={() => setGlossaryOpen((o) => !o)}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              {glossaryOpen ? <Minus className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
              {glossaryOpen ? "收起" : "展开"}
            </button>
          }
        >
          {glossaryOpen && (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
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
        </Section>

        {/* 底线：全程本地 + 版权红线 */}
        <Card as="section" className="flex flex-col gap-3 p-5 text-xs leading-5 text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 shrink-0 text-primary" />
            <span>全程本地处理，不上传任何云端。只克隆自己或已授权的声音。</span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Users className="h-4 w-4 text-primary" />
            <Link to="/voices?tab=market" className="font-medium text-primary transition hover:opacity-80">也可以试试预置的开源音色</Link>
          </div>
        </Card>
        </div>
      </PageShell>
    </div>
  )
}