import {
  ArrowRight, AudioLines, CheckCircle2, FileAudio, Info, Library,
  Mic, MonitorPlay, Radar, Scissors, Sparkles, Volume2, Waves,
} from "lucide-react"
import { Link } from "react-router-dom"

/** 四种音源入口：与实现一一对应（音色库页三个入口 + 桌宠内录 + 工坊手动） */
const SOURCES = [
  {
    icon: FileAudio,
    title: "视频 / 音频文件",
    desc: "单选或多选 mp4、mp3、wav 等文件，也支持整个文件夹批量导入。",
    where: "音色库 · 挖掘卡片",
    highlight: false,
  },
  {
    icon: Mic,
    title: "麦克风录音",
    desc: "点一下开始录、再点停止，录完自动进入解析与挖掘，适合录自己的声音。",
    where: "音色库 · 挖掘卡片",
    highlight: false,
  },
  {
    icon: MonitorPlay,
    title: "桌宠一键内录",
    desc: "刷抖音/看视频时右键桌宠，直接抓取系统正在播放的声音，不占麦克风、无混响噪声。",
    where: "桌宠右键菜单",
    highlight: true,
  },
  {
    icon: Scissors,
    title: "工坊手动勾选",
    desc: "想精挑细选？在音色工坊解析视频后手动勾片段，手工聚合建参考档案。",
    where: "音色工坊",
    highlight: false,
  },
]

/** 自动链路四步：抓取进来之后的一切都是自动的 */
const STEPS = [
  { icon: Volume2, title: "抓取音源", desc: "内录/上传/录音，统一落到素材库" },
  { icon: Waves, title: "分离人声", desc: "demucs 自动去掉 BGM 与音效" },
  { icon: Scissors, title: "静音切片", desc: "按停顿切成 3~10 秒纯净片段" },
  { icon: AudioLines, title: "聚类挖掘", desc: "转写 + 声纹聚类，筛出候选音色" },
]

export function DiscoverPage() {
  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">STAGE 01.5 / VOICE DISCOVERY</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            发掘音色
            <span className="ml-3 rounded-full border border-primary/40 bg-primary/10 px-3 py-1 align-middle font-mono text-xs font-medium text-primary">AUTO</span>
          </h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            声音无处不在：一段视频、一首歌的间隙、刷到的短视频。发掘音色把「听到 → 拥有」压缩成一次点击：
            任意渠道抓来的声音都会自动去 BGM、切片、转写聚类，筛出可用的候选音色——你只管试听和保存。
          </p>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-10 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {/* 音源入口 */}
        <section>
          <div className="mb-5 flex items-end justify-between">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">SOURCES</p>
              <h3 className="mt-2 text-2xl font-semibold text-foreground">四种音源，殊途同归</h3>
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            {SOURCES.map(({ icon: Icon, title, desc, where, highlight }) => (
              <article
                key={title}
                className={`relative rounded-2xl border p-5 shadow-md transition hover:shadow-lg ${
                  highlight ? "border-primary/60 bg-gradient-to-br from-primary/10 via-card to-card" : "border-border bg-card/85"
                }`}
              >
                {highlight && (
                  <span className="absolute right-4 top-4 rounded-full bg-primary/15 px-2.5 py-0.5 text-[10px] font-medium text-primary">
                    新 · 推荐
                  </span>
                )}
                <span className={`flex h-10 w-10 items-center justify-center rounded-lg border ${highlight ? "border-primary/50 bg-primary/10 text-primary" : "border-border bg-background text-muted-foreground"}`}>
                  <Icon className="h-5 w-5" />
                </span>
                <h4 className="mt-3 text-sm font-semibold text-card-foreground">{title}</h4>
                <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{desc}</p>
                <p className="mt-3 font-mono text-[11px] text-muted-foreground/80">入口：{where}</p>
              </article>
            ))}
          </div>
        </section>

        {/* 自动链路 */}
        <section>
          <div className="mb-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">PIPELINE</p>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">抓来之后，交给流水线</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">从原始声音到候选音色，四步全自动，全程本地处理。</p>
          </div>
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-md sm:p-6">
            <ol className="flex flex-col gap-4 sm:flex-row sm:items-stretch sm:gap-2">
              {STEPS.map(({ icon: Icon, title, desc }, i) => (
                <li key={title} className="flex flex-1 items-start gap-3 sm:flex-col sm:items-center sm:text-center">
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-primary/40 bg-primary/10 text-primary">
                    <Icon className="h-5 w-5" />
                  </span>
                  <div className="sm:mt-2">
                    <p className="text-sm font-medium text-card-foreground">
                      <span className="mr-1.5 font-mono text-[10px] text-muted-foreground">{String(i + 1).padStart(2, "0")}</span>
                      {title}
                    </p>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">{desc}</p>
                  </div>
                  {i < STEPS.length - 1 && (
                    <ArrowRight className="hidden h-4 w-4 shrink-0 self-center text-muted-foreground/40 sm:ml-1 sm:block" />
                  )}
                </li>
              ))}
            </ol>
            <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-border pt-4 text-xs text-muted-foreground">
              <Sparkles className="h-3.5 w-3.5 text-primary" />
              挖出的每个候选都会用一句与素材无关的新文本合成试听，真实检验音色迁移效果——像不像，一听便知。
            </div>
          </div>
        </section>

        {/* 质量说明 */}
        <section className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-md sm:p-6">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">QUALITY</p>
            <h3 className="mt-2 text-lg font-semibold text-card-foreground">为什么内录比麦克风更值得用？</h3>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/5 p-4">
                <p className="flex items-center gap-2 text-sm font-medium text-emerald-400">
                  <CheckCircle2 className="h-4 w-4" />系统内录（桌宠一键）
                </p>
                <ul className="mt-2.5 space-y-1.5 text-xs leading-5 text-muted-foreground">
                  <li>· 直接抓声卡数字信号，与原声一致</li>
                  <li>· 无房间混响、无环境噪声</li>
                  <li>· 不占用麦克风，看视频时无感录制</li>
                </ul>
              </div>
              <div className="rounded-xl border border-border bg-background/60 p-4">
                <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
                  <Mic className="h-4 w-4" />麦克风外放录制
                </p>
                <ul className="mt-2.5 space-y-1.5 text-xs leading-5 text-muted-foreground">
                  <li>· 声波二次传播，混响与噪声明显</li>
                  <li>· 只推荐用来录「你自己的声音」</li>
                  <li>· 录别人说话请用内录，效果天差地别</li>
                </ul>
              </div>
            </div>
            <p className="mt-4 flex items-start gap-2 text-xs leading-5 text-muted-foreground">
              <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
              内录取决于系统实际播放：系统静音时抓不到内容；蓝牙耳机下抓的是耳机里正在播的信号，同样可用。
            </p>
          </div>
          <div className="space-y-4">
            <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-md">
              <p className="font-mono text-xs uppercase tracking-widest text-primary">TIPS</p>
              <h4 className="mt-2 text-sm font-semibold text-card-foreground">挖得准的三个习惯</h4>
              <ol className="mt-3 space-y-2 text-xs leading-5 text-muted-foreground">
                <li className="flex gap-2"><span className="font-mono text-primary">1</span>素材里只有一个人说话时效果最好；多人视频挖完记得筛掉别人的候选。</li>
                <li className="flex gap-2"><span className="font-mono text-primary">2</span>相似度阈值调高挖出更多不同音色，最小簇人数能滤掉零散噪声。</li>
                <li className="flex gap-2"><span className="font-mono text-primary">3</span>一次挖不满意就换段素材再挖，候选会持续累积。</li>
              </ol>
            </div>
            <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-md">
              <p className="font-mono text-xs uppercase tracking-widest text-primary">PRIVACY</p>
              <h4 className="mt-2 text-sm font-semibold text-card-foreground">全程本地</h4>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">
                录音、解析、挖掘都在这台电脑上完成，不上传任何云端。请继续遵守项目红线：只克隆自己或已授权的声音。
              </p>
            </div>
          </div>
        </section>

        {/* 去哪用 */}
        <section className="grid gap-4 sm:grid-cols-2">
          <Link
            to="/voices"
            className="group flex items-center justify-between rounded-2xl border border-primary/50 bg-gradient-to-br from-primary/15 via-card to-card p-5 shadow-md transition hover:shadow-lg"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-primary/50 bg-primary/10 text-primary">
                <Library className="h-5 w-5" />
              </span>
              <div>
                <p className="text-sm font-semibold text-card-foreground">去音色库开始挖掘</p>
                <p className="mt-0.5 text-xs text-muted-foreground">选文件 / 录音 / 调参数，一站式完成</p>
              </div>
            </div>
            <ArrowRight className="h-4 w-4 text-primary transition group-hover:translate-x-1" />
          </Link>
          <Link
            to="/workshop"
            className="group flex items-center justify-between rounded-2xl border border-border bg-card/85 p-5 shadow-md transition hover:shadow-lg"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-lg border border-border bg-background text-muted-foreground">
                <Radar className="h-5 w-5" />
              </span>
              <div>
                <p className="text-sm font-semibold text-card-foreground">去音色工坊解析视频</p>
                <p className="mt-0.5 text-xs text-muted-foreground">切片质检、手动勾选、精修参考档案</p>
              </div>
            </div>
            <ArrowRight className="h-4 w-4 text-muted-foreground transition group-hover:translate-x-1" />
          </Link>
        </section>
      </main>
    </div>
  )
}
