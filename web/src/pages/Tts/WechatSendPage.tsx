import {
  Check,
  CircleAlert,
  Clock,
  Hand,
  Loader2,
  MessageCircle,
  Mic,
  RefreshCw,
  Send,
  Volume2,
  Wand2,
} from "lucide-react"
import { Link } from "react-router-dom"
import { PageShell } from "@/components/layout/PageShell"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { ErrorPanel } from "@/components/ErrorPanel"
import { mediaUrl } from "@/api/client"
import type { useWechatSend } from "@/pages/Tts/useWechatSend"

function fmtTs(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false })
  } catch {
    return String(ts)
  }
}

export function WechatSendPage(p: ReturnType<typeof useWechatSend>) {
  const sending = p.busy === "send"
  const playing = p.busy === "play"
  const manualing = p.busy === "manual"
  const busy = p.busy !== ""
  const ttsReady = Boolean(p.lastTts?.ok && p.lastTts?.wav)
  const sendEta = p.lastTts?.duration_s ? Math.ceil(p.lastTts.duration_s + 3) : 10

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      

      <PageShell className="grid gap-10 lg:grid-cols-[minmax(0,1.6fr)_360px]">
        <section className="space-y-8">
          {/* 发送内容 */}
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-primary">发送内容</p>
                <h3 className="mt-2 text-lg font-semibold text-card-foreground">最近一次合成产物</h3>
              </div>
              <button
                type="button"
                onClick={() => void p.refresh()}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
              >
                <RefreshCw className="h-3.5 w-3.5" />刷新
              </button>
            </div>
            {ttsReady ? (
              <div className="mt-4">
                <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                  <span className="font-mono text-foreground">{p.lastTts?.wav}</span>
                  <span className="inline-flex items-center gap-1">
                    <Clock className="h-3.5 w-3.5" />{p.lastTts?.duration_s}s
                  </span>
                </div>
                <StudioAudioPlayer src={mediaUrl(p.lastTts?.url ?? "")} label="试听发送内容" className="mt-3" />
                <p className="mt-2 text-xs text-muted-foreground">
                  不满意？切到「单段合成」重新生成一条，这里会自动更新。
                </p>
              </div>
            ) : (
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
                <span>还没有可发送的合成语音。</span>
                <Link to="/tts?tab=single" className="shrink-0 font-medium underline underline-offset-2">
                  去单段合成 →
                </Link>
              </div>
            )}
          </div>

          {/* 发送方式 */}
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <p className="text-xs font-medium text-primary">发送方式</p>
            <h3 className="mt-2 text-lg font-semibold text-card-foreground">三档路径，按需选择</h3>

            {/* 预热进度：模型常驻化后才能快，讲清楚「第一次为什么慢」 */}
            {p.warming && (
              <div className="mt-4 rounded-lg border border-yellow-500/40 bg-yellow-500/10 p-3.5">
                <div className="flex items-start gap-2.5">
                  <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-yellow-600" />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-semibold text-yellow-700">
                      模型预热中 {p.warmup?.steps.length ?? 0}/3 · 首次发送需等待
                    </p>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                      正在把 TTS / RVC / 播放服务常驻到显存（合计约 50s）。预热完成后发送最快；
                      现在点发送也会等预热结束自动继续，不会失败。
                    </p>
                    {!!p.warmup?.steps.length && (
                      <ul className="mt-2 space-y-1">
                        {p.warmup.steps.map((s, i) => (
                          <li
                            key={`${s.step}-${i}`}
                            className="flex items-center gap-1.5 text-xs text-muted-foreground"
                          >
                            {s.ok ? (
                              <Check className="h-3.5 w-3.5 shrink-0 text-primary" />
                            ) : (
                              <CircleAlert className="h-3.5 w-3.5 shrink-0 text-destructive" />
                            )}
                            <span>{s.step}</span>
                            <span className="font-mono">{s.seconds}s</span>
                            <span>{s.ok ? "就绪" : "失败"}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              </div>
            )}
            {!p.warming && p.warmup?.done && (
              <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
                <Check className="h-3.5 w-3.5 text-primary" />
                模型已就绪
                {p.warmup.total_s ? `（预热 ${Math.round(p.warmup.total_s)}s）` : ""}，现在发送最快。
              </p>
            )}

            <div className="mt-5 space-y-4">
              {/* ① 全自动 */}
              <div className="rounded-lg border border-primary/30 bg-primary/5 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-primary/15 text-primary">
                      <Send className="h-4 w-4" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold text-card-foreground">① 全自动发送（推荐）</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">
                        自动切录音设备 → 前台化微信模拟按住 Alt → 播放 → 松开发送，全程无需动手。
                        执行期间（约 {sendEta}s）<span className="font-medium text-foreground">不要动键鼠</span>，
                        微信会被抢前台。
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    disabled={!p.backendUp || busy || !ttsReady}
                    onClick={() => void p.sendAuto(p.lastTts?.wav)}
                    className="inline-flex shrink-0 items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-xs font-medium text-primary-foreground shadow-md transition hover:scale-105 disabled:pointer-events-none disabled:opacity-50"
                  >
                    {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                    {sending ? `发送中（约 ${sendEta}s）…` : "自动发送到微信"}
                  </button>
                </div>
              </div>

              {/* ② 半自动 */}
              <div className="rounded-lg border border-border bg-background/60 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                      <Volume2 className="h-4 w-4" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold text-card-foreground">② 半自动 · 播放到虚拟声卡</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">
                        点按钮后先播 2 秒静音头——利用这个时间到微信<span className="font-medium text-foreground">按住 Alt（或点话筒）</span>
                        开始录音，播完松开即发送。全自动方式在你的微信上不生效时用这档。
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    disabled={!p.backendUp || busy || !ttsReady}
                    onClick={() => void p.playToCable(p.lastTts?.wav)}
                    className="inline-flex shrink-0 items-center gap-2 rounded-md border border-border bg-background px-4 py-2.5 text-xs font-medium text-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    {playing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Volume2 className="h-4 w-4" />}
                    {playing ? "播放中…快去按 Alt" : "开始播放"}
                  </button>
                </div>
              </div>

              {/* ③ 手动实时 */}
              <div className="rounded-lg border border-border bg-background/60 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="flex items-start gap-3">
                    <span className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
                      <Mic className="h-4 w-4" />
                    </span>
                    <div>
                      <p className="text-sm font-semibold text-card-foreground">③ 手动 · 实时变声说话</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">
                        启动实时变声引擎并把微信录音切到虚拟声卡，然后去微信按住 Alt
                        <span className="font-medium text-foreground">用你自己的嘴说话</span>，说完松开发送。
                        不合成文本，适合即兴聊天。
                      </p>
                    </div>
                  </div>
                  <button
                    type="button"
                    disabled={!p.backendUp || busy}
                    onClick={() => void p.manualSetup()}
                    className="inline-flex shrink-0 items-center gap-2 rounded-md border border-border bg-background px-4 py-2.5 text-xs font-medium text-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    {manualing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
                    {manualing ? "启动中…" : "启动实时变声"}
                  </button>
                </div>
              </div>
            </div>

            {!p.backendUp && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
                <CircleAlert className="h-4 w-4" />本地服务离线，请确认后端已启动。
              </div>
            )}
            {p.errorMessage && <ErrorPanel title="微信发送失败" detail={p.errorMessage} />}
            {p.lastResult?.ok && p.lastResult.hint && (
              <div className="mt-5 rounded-md border border-primary/30 bg-primary/10 px-3 py-2.5 text-xs leading-5 text-foreground">
                <p className="font-medium">{p.lastResult.hint}</p>
                {p.lastResult.hint2 && <p className="mt-1 text-muted-foreground">{p.lastResult.hint2}</p>}
                {p.lastResult.warn && <p className="mt-1 text-yellow-600">{p.lastResult.warn}</p>}
              </div>
            )}
            {sending && (
              <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
                <Hand className="h-3.5 w-3.5" />发送期间请离开键鼠，微信会自动抢前台完成录音与发送。
              </p>
            )}
          </div>
        </section>

        {/* 发送历史 */}
        <section>
          <div className="sticky top-[116px] lg:top-24 rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6">
            <p className="text-xs font-medium text-primary">发送历史</p>
            <h3 className="mt-2 flex items-center gap-2 text-xl font-semibold text-card-foreground">
              <MessageCircle className="h-4 w-4 text-primary" />
              {p.history.length ? `${p.history.length} 条` : "待发送"}
            </h3>
            {p.history.length ? (
              <ul className="mt-5 max-h-[60vh] space-y-3 overflow-y-auto pr-1">
                {p.history.map((it, i) => (
                  <li key={`${it.ts}-${i}`} className="rounded-lg border border-border bg-background/60 p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs text-muted-foreground">{fmtTs(it.ts)}</span>
                      <span
                        className={`font-mono text-xs ${
                          it.outcome === "ok" ? "text-primary" : "text-destructive"
                        }`}
                      >
                        {it.outcome === "ok" ? "发送成功" : it.outcome}
                      </span>
                    </div>
                    <div className="mt-1.5 flex items-center justify-between gap-2">
                      <span className="line-clamp-1 font-mono text-xs text-card-foreground">
                        {it.wav} · {it.duration_s}s
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void p.sendAuto(it.wav)}
                        className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
                      >
                        重发
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-5 text-xs leading-5 text-muted-foreground">
                还没有发送记录。第一次成功发送后，这里会列出每条语音的时间和状态，点「重发」可以原样再发一条。
              </p>
            )}
          </div>
        </section>
      </PageShell>
    </div>
  )
}
