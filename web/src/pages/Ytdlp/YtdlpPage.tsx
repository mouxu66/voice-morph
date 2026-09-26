import { CircleAlert, CloudDownload, Loader2, TriangleAlert } from "lucide-react"
import type { useYtdlp } from "@/pages/Ytdlp/useYtdlp"
import { ErrorPanel } from "@/components/ErrorPanel"
import { PageShell } from "@/components/layout/PageShell"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"

/**
 * 「在线扒歌」页：粘一条平台链接，交给你自己装的 yt-dlp 把音频拉回会话目录。
 *
 * 版式与「翻唱」页刻意不同：那边是"操作台"（左边输入右边参数），这边是
 * **单一动作页** —— 一个输入框、一个按钮、一个结果。因为这个页面只做一件事，
 * 加装饰性分区只会让人以为还有别的设置。
 *
 * 三块内容按重要性排：
 *   1. 环境状态（yt-dlp 装没装）—— 没装的话下面全是白搭，必须最先说清；
 *   2. 输入与结果 —— 主体；
 *   3. 边界说明（这个工具会失控在哪、法律边界）—— 折叠在底部，不抢视线但不藏。
 */
export function YtdlpPage(p: ReturnType<typeof useYtdlp>) {
  const notReady = !p.probing && !p.ready

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <PageShell className="space-y-8">
        <header>
          <p className="text-xs font-medium text-primary">在线扒歌</p>
          <h2 className="mt-2 text-2xl font-semibold text-foreground">粘一条平台链接，把歌取回来</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            支持
            <strong className="text-card-foreground">网易云音乐、QQ音乐、B站、咪咕、喜马拉雅</strong>
            等站点的分享链接。取回的音频只留在
            <strong className="text-card-foreground">本次会话</strong>
            里，退出即删 —— 接着去「翻唱」页就能直接用它换音色。
          </p>
        </header>

        {/* ① 环境状态：没装 yt-dlp 时，这一条就是全部答案，别让用户去猜输入框为什么点了没反应 */}
        {p.probing && (
          <div className="flex items-center gap-2 rounded-md border border-border bg-background/50 px-3 py-2.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            正在检查 yt-dlp…
          </div>
        )}
        {notReady && (
          <div className="rounded-xl border border-amber-500/40 bg-amber-500/5 p-4">
            <div className="flex items-start gap-2.5">
              <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
              <div className="min-w-0">
                <p className="text-sm font-medium text-card-foreground">没检测到 yt-dlp</p>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">
                  {p.status?.hint ||
                    "这个功能靠外部的 yt-dlp 取歌，本项目不内置它。装好后重启后端即可。"}
                </p>
                <p className="mt-2 text-xs leading-5 text-muted-foreground">
                  它只是一个可执行文件，放在任意目录都行；也可以用环境变量指定路径。装法见
                  yt-dlp 官方仓库的 Releases 页。{" "}
                  <span className="text-card-foreground">装好之后记得偶尔升级它</span>
                  （平台一改版，旧版本就会认不出链接）。
                </p>
              </div>
            </div>
          </div>
        )}

        {p.errorMessage && <ErrorPanel title="拉取失败" detail={p.errorMessage} />}
        {p.feedback && (
          <div className="flex items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2.5 text-xs text-primary animate-in fade-in slide-in-from-top-2 duration-300">
            <CircleAlert className="h-3.5 w-3.5" />
            {p.feedback}
          </div>
        )}

        {/* ② 输入 + 结果 */}
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <p className="text-sm font-semibold text-card-foreground">歌曲链接</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            在音乐 App 里点「分享 → 复制链接」，粘到这里就行。歌单/合集链接只会取第一首。
          </p>

          <div className="mt-3 flex items-center gap-2">
            <input
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
              disabled={p.fetching || notReady}
              className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 font-mono text-xs text-card-foreground placeholder:text-muted-foreground/60 disabled:opacity-50"
              aria-label="歌曲链接"
            />
            <button
              type="button"
              onClick={() => void p.fetchFromUrl()}
              disabled={p.fetching || notReady || !p.url.trim()}
              className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20 disabled:pointer-events-none disabled:opacity-40"
            >
              {p.fetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CloudDownload className="h-3.5 w-3.5" />}
              {p.fetching ? "取回中…" : "取回并试听"}
            </button>
          </div>

          {p.preview && (
            <div className="mt-3 rounded-lg border border-primary/30 bg-primary/5 p-2">
              <div className="flex items-center justify-between gap-2 px-1 text-[11px] text-muted-foreground">
                <span className="truncate">
                  {p.fetchedSite && <span className="text-card-foreground">{p.fetchedSite} · </span>}
                  {p.fetchedName}
                </span>
                <button
                  type="button"
                  onClick={p.clear}
                  className="shrink-0 underline-offset-2 hover:text-foreground hover:underline"
                >
                  丢弃
                </button>
              </div>
              <div className="mt-1.5">
                <StudioAudioPlayer src={p.preview} />
              </div>
              <p className="mt-1.5 px-1 text-[11px] leading-4 text-muted-foreground">
                {p.summary} · 确认是这首歌之后，去「翻唱」页就能直接用它（同一次会话里不用再传一次）。
              </p>
            </div>
          )}

          {/* 支持的站点从后端清单渲染 —— 后端放行哪些，这里就显示哪些，不两处各写一份 */}
          {p.status?.sites?.length ? (
            <div className="mt-4 border-t border-border pt-3">
              <p className="text-[11px] font-medium text-muted-foreground">目前放行的站点</p>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {p.status.sites.map((s) => (
                  <span
                    key={s.example}
                    title={s.example}
                    className="rounded-md border border-border bg-background/60 px-2 py-1 text-[11px] text-muted-foreground"
                  >
                    {s.name}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
        </section>

        {/* ③ 边界说明：不抢视线，但不藏 */}
        <section className="rounded-xl border border-border bg-background/40 p-4">
          <p className="text-xs font-medium text-card-foreground">关于这个功能，有两件事得说清楚</p>
          <ul className="mt-2 space-y-1.5 text-[11px] leading-5 text-muted-foreground">
            <li>
              <span className="text-card-foreground">它是外部工具，不是本项目的解析器。</span>
              yt-dlp 由社区维护、不在本仓库里，本项目只负责调用它。平台改版后旧版本会失效，
              跑一次升级命令即可 —— 这也正是它比硬编码解析接口耐用的原因。
            </li>
            <li>
              <span className="text-card-foreground">请只取你有权使用的音频。</span>
              工具本身中立，但下载正版音乐仍受平台条款与著作权法约束。用于个人学习、
              自己的素材整理没问题；付费/会员曲目拿不到音频流是**正常结果**，不是 bug。
            </li>
          </ul>
        </section>
      </PageShell>
    </div>
  )
}
