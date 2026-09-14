import { ArrowRight, AudioLines, HelpCircle, Mic2, Minus, Plus, Radio, ShieldCheck, Sparkles, Speech, Users, Volume2, Wrench } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router-dom"
import { EffectLadderCard } from "@/components/EffectLadderCard"
import { useAppStore } from "@/store/useAppStore"

/** 三张主路径 CTA：首屏就给用户"我该从哪条路走"的判断 */
const PATHS = [
  {
    to: "/tts?tab=single",
    icon: Speech,
    title: "免训练变声",
    tag: "最快 · 10 秒听到效果",
    desc: "输一段字，马上用指定音色读出来。最适合先尝尝「变声」到底是什么感觉。",
    tone: "primary" as const,
  },
  {
    to: "/workshop",
    icon: Mic2,
    title: "训练变声",
    tag: "最像 · 攒素材练出专属嗓子",
    desc: "从一条素材开始，一步步养成你的专属音色，最后能开着麦实时变声。",
    tone: "accent" as const,
  },
  {
    to: "/offlinevc",
    icon: Wrench,
    title: "工具箱",
    tag: "进阶 · 整段变声 / 效果器",
    desc: "把录好的整段音频一次性变声、叠加混响电音，适合后期配音与剪辑。",
    tone: "default" as const,
  },
]

/** 「第一条音色」最小闭环：5 步，每步讲清"做什么 + 为什么" */
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

  // 根据全局音色数据算"效果阶梯当前位置"（零后端改动，App 已 5s 轮询填充 voices）
  const hasRef = voices.some((v) => v.has_reference !== false && (v.has_reference === true || v.duration_s >= 3))
  const modelReady = voices.some((v) => v.model_ready === true)
  const longDataset = voices.some((v) => (v.dataset_count ?? 0) > 0)
  const totalSeconds = voices.reduce((sum, v) => sum + (v.duration_s || 0), 0)
  const currentLevel = modelReady ? 3 : longDataset ? 2 : hasRef ? 1 : 0

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      {/* Hero：一句话讲清这是干嘛的，首屏放三大入口 */}
      <header className="relative overflow-hidden border-b border-border px-5 py-12 sm:px-8 lg:px-12 lg:py-16">
        <div className="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary/10 via-background to-card" aria-hidden="true" />
        <div className="relative mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">HOME / 零基础入门</p>
          <h2 className="mt-4 max-w-3xl font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            让任何声音，都变成你的
          </h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            这不是塞给你一堆工具，而是从一条素材开始，一步一步带你走到最后——
            能在微信里用真声说话、开着麦打游戏。往下滑，跟着做就行。
          </p>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-12 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {/* 三大主路径：首屏跳板 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">THREE PATHS</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">三条路，先想清楚你要哪种</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">不确定？就从「免训练变声」开始——最快尝到甜头，任何时刻都能回来换跑道。</p>
          </div>
          <div className="grid gap-4 md:grid-cols-3">
            {PATHS.map(({ to, icon: Icon, title, tag, desc, tone }) => (
              <Link
                key={title}
                to={to}
                className={`group rounded-2xl border p-5 shadow-md transition hover:-translate-y-0.5 hover:shadow-lg ${
                  tone === "primary"
                    ? "border-primary/50 bg-gradient-to-br from-primary/15 via-card to-card"
                    : tone === "accent"
                      ? "border-primary/30 bg-card/85"
                      : "border-border bg-card/85"
                }`}
              >
                <span className={`flex h-10 w-10 items-center justify-center rounded-lg border ${tone === "primary" ? "border-primary/50 bg-primary/10 text-primary" : "border-border bg-background text-muted-foreground"}`}>
                  <Icon className="h-5 w-5" />
                </span>
                <h4 className="mt-3 text-sm font-semibold text-card-foreground">{title}</h4>
                <p className="mt-1 inline-block rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">{tag}</p>
                <p className="mt-2.5 text-xs leading-5 text-muted-foreground">{desc}</p>
                <span className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-primary opacity-0 transition group-hover:opacity-100">
                  从这里开始 <ArrowRight className="h-3.5 w-3.5" />
                </span>
              </Link>
            ))}
          </div>
        </section>

        {/* 最小闭环：第一条音色的完整教程 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">FIRST VOICE</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">跟着做：你的第一条音色</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">五步走完最小闭环。每一屏顶部都有导览小助手，不懂的词随时点开下面的白话解释。</p>
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