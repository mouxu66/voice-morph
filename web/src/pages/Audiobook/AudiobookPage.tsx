import {
  BookOpen,
  CircleAlert,
  Download,
  FileUp,
  ListMusic,
  Loader2,
  Sparkles,
  Square,
} from "lucide-react"
import { Link } from "react-router-dom"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { mediaUrl } from "@/api/client"
import type { useAudiobook } from "@/pages/Audiobook/useAudiobook"

export function AudiobookPage(p: ReturnType<typeof useAudiobook>) {
  const st = p.status
  const selectedName =
    p.voices.find((v) => v.id === p.selectedVoiceId)?.display_name ?? p.selectedVoiceId
  const finished = st && !st.running && (st.status === "done" || st.status === "error") && st.url

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">STAGE 05 / AUDIOBOOK</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">有声书工作台</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            粘贴长文或导入 SRT 字幕，选一个音色逐句合成并自动拼接成完整音频；SRT 会按时间轴保留原节奏，支持逐句试听。
          </p>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-[minmax(0,1.6fr)_360px] lg:px-12 lg:py-14">
        <section className="space-y-8">
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="font-mono text-xs uppercase tracking-widest text-primary">任务输入</p>
                <h3 className="mt-2 text-lg font-semibold text-card-foreground">长文 / SRT → 有声书</h3>
              </div>
              {p.text.trim() && (
                <span className={`rounded-full px-2.5 py-0.5 text-xs ${p.isSrt ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground"}`}>
                  {p.isSrt ? "SRT 模式 · 按时间轴对齐" : "长文模式 · 按句切分"}
                </span>
              )}
            </div>

            <div className="mt-5">
              <label htmlFor="ab-voice" className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <BookOpen className="h-3.5 w-3.5" />朗读者音色
              </label>
              {p.voices.length ? (
                <select
                  id="ab-voice"
                  value={p.selectedVoiceId ?? ""}
                  onChange={(e) => p.selectVoice(e.target.value)}
                  className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  {p.voices.map((v) => (
                    <option key={v.id} value={v.id}>{v.display_name ?? v.id}</option>
                  ))}
                </select>
              ) : (
                <div className="mt-2 flex items-center justify-between gap-3 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
                  <span>还没有可用音色。</span>
                  <Link to="/voices" className="shrink-0 font-medium underline underline-offset-2">去音色库挖掘 →</Link>
                </div>
              )}
              {selectedName && <p className="mt-2 text-xs text-muted-foreground">当前音色：{selectedName}</p>}
            </div>

            <textarea
              value={p.text}
              onChange={(e) => p.setText(e.target.value)}
              rows={12}
              placeholder={"粘贴整篇文稿，或直接把 .srt 文件内容贴进来（识别到时间轴 --> 自动切 SRT 模式）\n示例：第一章 雾都之夜。雨点敲打着窗棂，他推开那扇沉重的门……"}
              className="mt-5 w-full resize-y rounded-md border border-border bg-background px-3 py-2.5 font-mono text-sm leading-6 text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
            />

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-3 py-2 text-xs font-medium text-muted-foreground transition hover:border-primary hover:text-primary">
                <FileUp className="h-3.5 w-3.5" />导入 .srt / .txt
                <input
                  type="file"
                  accept=".srt,.txt"
                  className="hidden"
                  onChange={(e) => { p.loadFile(e.target.files?.[0]); e.target.value = "" }}
                />
              </label>
              <span className="font-mono text-xs text-muted-foreground">{p.text.length} 字</span>
            </div>

            {!p.isSrt && (
              <div className="mt-5">
                <label htmlFor="ab-gap" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>句间停顿</span>
                  <span className="font-mono text-primary">{p.gapMs} ms</span>
                </label>
                <input
                  id="ab-gap"
                  type="range"
                  min={0}
                  max={1500}
                  step={50}
                  value={p.gapMs}
                  onChange={(e) => p.setGapMs(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]"
                />
              </div>
            )}

            {!p.backendUp && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
                <CircleAlert className="h-4 w-4" />本地服务离线，请确认后端已启动。
              </div>
            )}
            {p.errorMessage && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
                <CircleAlert className="h-4 w-4" />{p.errorMessage}
              </div>
            )}
            {st?.error && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
                <CircleAlert className="h-4 w-4" />{st.error}
              </div>
            )}

            {st?.running && (
              <div className="mt-5 rounded-lg border border-border bg-background/60 p-4">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium text-foreground">
                    {st.mode === "srt" ? "SRT 逐句合成" : "逐句合成"} · {st.done}/{st.total} 句
                  </span>
                  <span className="font-mono text-primary">{st.percent}%</span>
                </div>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
                  <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${st.percent}%` }} />
                </div>
                {st.current_text && (
                  <p className="mt-2 line-clamp-1 text-xs text-muted-foreground">正在合成：{st.current_text}</p>
                )}
              </div>
            )}

            <div className="mt-6 flex flex-wrap gap-3">
              <button
                type="button"
                disabled={!p.canStart}
                onClick={() => void p.start()}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
              >
                {p.submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                {p.submitting ? "提交中…" : "开始合成"}
              </button>
              {p.running && (
                <button
                  type="button"
                  onClick={() => void p.cancel()}
                  className="inline-flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm font-medium text-destructive transition hover:bg-destructive/20"
                >
                  <Square className="h-4 w-4" />停止任务
                </button>
              )}
            </div>
            {p.running && (
              <p className="mt-3 text-xs leading-5 text-muted-foreground">
                合成期间可离开本页，任务在后台进行；回来后进度自动恢复。每句约需十几秒，全书请耐心等待。
              </p>
            )}
          </div>

          {finished && (
            <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="font-mono text-xs uppercase tracking-widest text-primary">成品</p>
                  <h3 className="mt-2 text-lg font-semibold text-card-foreground">拼接完成 · 全长 {st.duration_s}s</h3>
                </div>
                <a
                  href={mediaUrl(st.url)}
                  download={`audiobook-${st.voice_id}.wav`}
                  className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20"
                >
                  <Download className="h-4 w-4" />下载完整音频
                </a>
              </div>
              <StudioAudioPlayer src={mediaUrl(st.url)} label="播放有声书" className="mt-4" />
            </div>
          )}
        </section>

        <section>
          <div className="sticky top-24 rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">逐句明细</p>
            <h3 className="mt-2 flex items-center gap-2 text-xl font-semibold text-card-foreground">
              <ListMusic className="h-4 w-4 text-primary" />{st?.segments.length ? `${st.segments.length} 句` : "待合成"}
            </h3>
            {st?.segments.length ? (
              <ul className="mt-5 max-h-[60vh] space-y-3 overflow-y-auto pr-1">
                {st.segments.map((seg) => (
                  <li key={seg.i} className={`rounded-lg border p-3 ${seg.failed ? "border-destructive/40 bg-destructive/5" : "border-border bg-background/60"}`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs text-primary">{String(seg.i).padStart(3, "0")}</span>
                      <span className={`font-mono text-xs ${seg.failed ? "text-destructive" : "text-muted-foreground"}`}>
                        {seg.failed ? "合成失败" : `${seg.duration_s}s`}
                      </span>
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-card-foreground">{seg.text}</p>
                    {!seg.failed && seg.url && <StudioAudioPlayer src={mediaUrl(seg.url)} label={`第 ${seg.i} 句`} className="mt-2" />}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-5 text-xs leading-5 text-muted-foreground">提交任务后，每句合成的独立音频会出现在这里，可逐句试听检查。</p>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}
