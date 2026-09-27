import { useRef } from "react"
import {
  Check,
  CircleAlert,
  CloudDownload,
  Link2,
  Loader2,
  Music4,
  RefreshCw,
  ShieldCheck,
  Wrench,
} from "lucide-react"
import type { YtdlpStatus } from "@/api/client"
import type { useYtdlp } from "@/pages/Ytdlp/useYtdlp"
import { ErrorPanel } from "@/components/ErrorPanel"
import { Card, PageShell, Section } from "@/components/layout/PageShell"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { cn } from "@/lib/utils"

/**
 * 「在线扒歌」页。
 *
 * 这个页面只有一个动作：把一条平台链接变成会话目录里的一个音频文件。
 * 版式围绕"**先确认路通不通，再粘链接**"这条真实路径设计 —— 探活不是装饰性的
 * 状态灯泡，它是第一屏的主角：没装 yt-dlp 时下面整块输入区就是死路，
 * 与其让用户粘完链接再收到报错，不如一进来就说清"路不通，这么修"。
 *
 * 环境状态做成一枚**仪表盘式的主卡**，右侧那枚大字符号（`✓` / `✕` / `…`）
 * 是整页唯一的视觉重音，其余一律压平。这是刻意的：工具页不该有第二个抢视线的东西。
 */

/** 把探活结果收敛成三种可渲染的状态，避免模板里到处写三目。 */
type EnvState = "checking" | "ready" | "missing"

export function YtdlpPage(p: ReturnType<typeof useYtdlp>) {
  const inputRef = useRef<HTMLInputElement>(null)
  const env: EnvState = p.probing ? "checking" : p.ready ? "ready" : "missing"
  const locked = env !== "ready"

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <PageShell className="space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <p className="mb-1.5 flex items-center gap-2 text-xs font-medium text-primary">
              <span className="h-3 w-0.5 rounded-full bg-primary/70" aria-hidden="true" />
              在线扒歌
            </p>
            <h1 className="text-xl font-semibold tracking-tight text-foreground">
              粘一条平台链接，把歌取回来
            </h1>
            <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted-foreground">
              取回的音频只留在<strong className="font-medium text-card-foreground">本次会话</strong>里，
              退出即删。确认是这首歌之后，去「翻唱」页直接就能用它换音色，不用再传一次。
            </p>
          </div>
        </header>

        {/* ① 环境状态：第一屏主角。没装 yt-dlp 时这块就是全部答案。 */}
        <EnvCard p={p} env={env} />

        {p.errorMessage && <ErrorPanel title="拉取失败" detail={p.errorMessage} />}
        {p.feedback && (
          <div className="flex items-center gap-2 rounded-xl border border-primary/30 bg-primary/5 px-3.5 py-2.5 text-xs text-primary animate-in fade-in slide-in-from-top-2 duration-300">
            <Check className="h-3.5 w-3.5 shrink-0" />
            {p.feedback}
          </div>
        )}

        {/* ② 输入 + 结果 */}
        <Section
          className="!mt-6"
          title="歌曲链接"
          desc="在音乐 App 里点「分享 → 复制链接」，粘到这里就行。歌单 / 合集链接只会取第一首。"
        >
          <Card className="p-5 sm:p-6">
            <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
              <div className="relative min-w-0 flex-1">
                <Link2
                  className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground/70"
                  aria-hidden="true"
                />
                <input
                  ref={inputRef}
                  type="url"
                  value={p.url}
                  onChange={(e) => p.setUrl(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault()
                      void p.fetchFromUrl()
                    }
                  }}
                  placeholder="https://music.163.com/song?id=… 或分享短链"
                  disabled={p.fetching || locked}
                  className={cn(
                    "w-full rounded-lg border border-border bg-background py-2.5 pl-9 pr-3 font-mono text-xs",
                    "text-card-foreground placeholder:text-muted-foreground/60",
                    "transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
                    "disabled:cursor-not-allowed disabled:opacity-50",
                  )}
                  aria-label="歌曲链接"
                />
              </div>
              <button
                type="button"
                onClick={() => void p.fetchFromUrl()}
                disabled={p.fetching || locked || !p.url.trim()}
                className={cn(
                  "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg px-4 py-2.5 text-xs font-medium",
                  "bg-primary text-primary-foreground shadow-sm transition",
                  "hover:brightness-110 active:translate-y-px",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 focus-visible:ring-offset-2 focus-visible:ring-offset-background",
                  "disabled:pointer-events-none disabled:opacity-40",
                )}
              >
                {p.fetching ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <CloudDownload className="h-3.5 w-3.5" />
                )}
                {p.fetching ? "取回中…" : "取回并试听"}
              </button>
            </div>

            {p.preview && (
              <div className="mt-4 rounded-xl border border-primary/30 bg-primary/[0.06] p-3 animate-in fade-in slide-in-from-top-2 duration-300">
                <div className="flex items-center justify-between gap-3 px-0.5">
                  <div className="flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground">
                    <Music4 className="h-3.5 w-3.5 shrink-0 text-primary" />
                    {p.fetchedSite && (
                      <span className="shrink-0 font-medium text-card-foreground">{p.fetchedSite}</span>
                    )}
                    <span className="truncate font-mono">{p.fetchedName}</span>
                  </div>
                  <button
                    type="button"
                    onClick={p.clear}
                    className="shrink-0 rounded text-[11px] text-muted-foreground underline-offset-2 transition hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
                  >
                    丢弃
                  </button>
                </div>
                <div className="mt-2">
                  <StudioAudioPlayer src={p.preview} />
                </div>
                <p className="mt-2 px-0.5 text-[11px] leading-4 text-muted-foreground">
                  <span className="font-mono">{p.summary}</span>
                  {" · "}
                  确认是这首歌之后，去「翻唱」页就能直接用它。
                </p>
                {/* 只在"后端改写过地址"时出现。粘短链进来的用户看到这一行才明白
                    为什么刚才那条链接能跑通 —— 也让"换条链接再试"有依据。 */}
                {p.fetchedSourceUrl && p.fetchedSourceUrl !== p.submittedUrl && (
                  <p className="mt-1.5 break-all px-0.5 font-mono text-[10px] leading-4 text-muted-foreground/70">
                    实际解析到 {p.fetchedSourceUrl}
                  </p>
                )}
              </div>
            )}

            {/* 支持的站点从后端清单渲染 —— 后端放行哪些，这里就显示哪些，不两处各写一份 */}
            {p.status?.sites?.length ? (
              <div className="mt-5 border-t border-border pt-4">
                <p className="text-[11px] font-medium text-muted-foreground">
                  目前放行 {p.status.sites.length} 个站点
                </p>
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {p.status.sites.map((s) => (
                    <span
                      key={s.example}
                      title={s.example}
                      className="rounded-md border border-border bg-background/60 px-2 py-1 font-mono text-[11px] text-muted-foreground transition hover:border-input hover:text-card-foreground"
                    >
                      {s.name}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
          </Card>
        </Section>

        {/* ③ 边界说明：不抢视线，但不藏 */}
        <BoundaryNotes />
      </PageShell>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  登录态一行                                                         */
/* ------------------------------------------------------------------ */

/**
 * 登录态。**为什么值得占一行**：QQ 音乐几乎全曲库都要登录态才发音频流
 * （实测报的是 `only available for registered users`，不是付费墙），
 * 所以"配没配"是这一页的高频事实 —— 而用户配完之后得有地方确认自己配对了，
 * 否则他只能靠"再拉一次看看"来验证，那是在拿失败当探针。
 *
 * 未配时给一档中性提示（不是警告）：不配也能用网易云、B站等站点，
 * 这不该被渲染成"你有问题"。
 */
function CookieNote({ c }: { c: YtdlpStatus["cookie"] | undefined }) {
  if (!c) return null
  const bad = !c.ok
  const what =
    c.mode === "file" ? "cookies 文件" : c.mode === "browser" ? `${c.detail} 的登录态` : "未配"
  return (
    <p className="mt-2 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 text-[11px] leading-5">
      <span className={cn("shrink-0", bad ? "text-amber-500" : "text-muted-foreground/80")}>
        登录态
      </span>
      <span className={cn("font-mono", bad ? "text-amber-500" : "text-muted-foreground")}>{what}</span>
      {c.mode === "none" && (
        <span className="text-muted-foreground/70">· QQ 音乐这类站点会要求登录，拉失败时会给配置办法</span>
      )}
      {bad && <span className="text-amber-500">· 这个文件不存在</span>}
    </p>
  )
}

/* ------------------------------------------------------------------ */
/*  环境状态主卡                                                       */
/* ------------------------------------------------------------------ */

function EnvCard({ p, env }: { p: ReturnType<typeof useYtdlp>; env: EnvState }) {
  // 三态共用一个骨架，只有"右侧符号 + 文案 + 动作"不同。
  // 合并不是偷懒 —— 分开写会让三种状态的版式慢慢漂移，用户每次进来都要重新认路。
  //
  // 着色克制：ready 态**不给整块染色**，只在左侧加一道品牌色细条。
  // 原因：这块卡和下面的「取回并试听」按钮是同色系的，整块染色会让两个紫色互相
  // 抢注意力，而这一页真正要用户点的只有那个按钮。状态只需"读得到"，不该"喊得响"。
  const tone =
    env === "ready"
      ? { ring: "border-primary/30", accent: "bg-primary", mark: "text-primary" }
      : env === "missing"
        ? { ring: "border-amber-500/40", accent: "bg-amber-500", mark: "text-amber-500" }
        : { ring: "border-border", accent: "bg-muted-foreground/40", mark: "text-muted-foreground" }

  return (
    <Card className={cn("relative overflow-hidden", tone.ring)}>
      {/* 左侧状态条：比整块染色安静，但仍然一眼能看出"这卡有状态" */}
      <div className={cn("absolute inset-y-0 left-0 w-0.5", tone.accent)} aria-hidden="true" />
      <div className="relative flex items-start gap-4 p-4 pl-5 sm:p-5 sm:pl-6">
        <div className="min-w-0 flex-1">
          {env === "checking" && (
            <>
              <p className="text-sm font-medium text-card-foreground">正在检查 yt-dlp…</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                这是一个只读的本地检查，不联网。
              </p>
            </>
          )}

          {env === "ready" && (
            <>
              <p className="text-sm font-medium text-card-foreground">已就位，可以开始扒歌</p>
              <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1.5 rounded border border-border bg-background/70 px-1.5 py-0.5 font-mono text-[11px] text-card-foreground">
                  yt-dlp
                </span>
                {p.status?.version && <span className="font-mono text-[11px]">{p.status.version}</span>}
                <span className="text-[11px]">外部工具，由你自行维护</span>
              </p>
              <CookieNote c={p.status?.cookie} />
            </>
          )}

          {env === "missing" && (
            <>
              <p className="text-sm font-medium text-card-foreground">没检测到 yt-dlp</p>
              <p className="mt-1.5 max-w-2xl text-xs leading-5 text-muted-foreground">
                {p.status?.hint ||
                  "这个功能靠外部的 yt-dlp 取歌，本项目不内置它。它只是一个可执行文件，放在任意目录都行。"}
              </p>
              <p className="mt-2 max-w-2xl text-xs leading-5 text-muted-foreground">
                装好之后点右边的
                <span className="text-card-foreground">重新检测</span>
                ，不用重启。平台一改版旧版本就会认不出链接，所以
                <span className="text-card-foreground">记得偶尔升级它</span>。
              </p>
            </>
          )}

          {/* 检测到的真实路径：环境信息，放最后一行。
              `break-all` 是必须的 —— 这条 Windows 路径一百多字符且没有空格，
              不加会在窄容器里直接顶破卡片边界（实测过）。 */}
          {env === "ready" && p.status?.path && (
            <p
              className="mt-2 break-all font-mono text-[10px] leading-4 text-muted-foreground/70"
              title={p.status.path}
            >
              {p.status.path}
            </p>
          )}
        </div>

        {/* 右侧符号 + 动作：整页唯一的视觉重音 */}
        <div className="flex shrink-0 flex-col items-center gap-2">
          <div
            className={cn("flex h-9 w-9 items-center justify-center rounded-full", tone.mark)}
            aria-hidden="true"
          >
            {env === "checking" ? (
              <Loader2 className="h-5 w-5 animate-spin" />
            ) : env === "ready" ? (
              <Check className="h-5 w-5" strokeWidth={2.5} />
            ) : (
              <CircleAlert className="h-5 w-5" />
            )}
          </div>
          <button
            type="button"
            onClick={p.reprobe}
            disabled={p.probing}
            title="重新检查本机的 yt-dlp"
            className={cn(
              "inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg border border-border bg-background/70 px-2.5 py-1.5",
              "text-[11px] font-medium text-muted-foreground transition",
              "hover:border-input hover:text-card-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60",
              "disabled:pointer-events-none disabled:opacity-50",
            )}
          >
            <RefreshCw className={cn("h-3 w-3", p.probing && "animate-spin")} />
            重新检测
          </button>
        </div>
      </div>
    </Card>
  )
}

/* ------------------------------------------------------------------ */
/*  边界说明                                                          */
/* ------------------------------------------------------------------ */

/**
 * 两条边界并排，而不是串成一列长文。
 *
 * 串成一列时（原版）它们读起来像免责声明，用户会直接跳过；并排成两张等宽卡、
 * 每张只讲一件事，才有人真的看。这里的"等宽"是有意的对称 —— 这两条的权重确实相同，
 * 不需要制造主次。
 *
 * 图标底色给一格极浅的品牌色：不放的话这两个图标是整页最暗的笔画，
 * 缩到 3.5 尺寸几乎看不见（上一版就是这个问题，截图看出来的）。
 */
function BoundaryNotes() {
  const notes = [
    {
      icon: Wrench,
      title: "它是外部工具，不是本项目的解析器",
      body: "yt-dlp 由社区维护、不在本仓库里，本项目只负责调用它。平台改版后旧版本会失效，跑一次升级命令即可。这也正是它比硬编码的解析接口耐用的原因。",
    },
    {
      icon: ShieldCheck,
      title: "请只取你有权使用的音频",
      body: "工具本身中立，但下载正版音乐仍受平台条款与著作权法约束。用于个人学习、整理自己的素材没问题；付费或会员曲目拿不到音频流是正常结果，不是故障。",
    },
  ]
  return (
    <section className="grid gap-4 md:grid-cols-2">
      {notes.map((n) => (
        <div key={n.title} className="rounded-xl border border-border bg-background/40 p-4">
          <div className="flex items-center gap-2.5">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
              <n.icon className="h-3.5 w-3.5" aria-hidden="true" />
            </span>
            <p className="text-xs font-medium text-card-foreground">{n.title}</p>
          </div>
          <p className="mt-2 text-[11px] leading-5 text-muted-foreground">{n.body}</p>
        </div>
      ))}
    </section>
  )
}
