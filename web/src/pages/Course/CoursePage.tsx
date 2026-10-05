import {
  CircleAlert,
  Download,
  FileUp,
  Loader2,
  Presentation,
  RefreshCw,
  Sparkles,
  Square,
} from "lucide-react"
import { useMemo, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { ErrorPanel } from "@/components/ErrorPanel"
import { mediaUrl } from "@/api/client"
import type { useCourse } from "@/pages/Course/useCourse"
import { voiceOptionLabel } from "@/lib/voiceLabel"

/**
 * 讲稿录课：写一遍讲稿 → 用自己的保存声音逐页生成旁白 → 逐页对齐导出。
 *
 * 与「有声书」（同页另一个 tab）的分工：有声书把整篇拼成一条大音频；
 * 本页**逐页独立导出** —— 每页的音轨插回课件对应页，页号是对齐的锚。
 * 口径按方案④写「拟应用/推广」：这里是备课提效工具，不承诺"已实现师范实训"。
 */
export function CoursePage(p: ReturnType<typeof useCourse>) {
  const st = p.status
  const selectedName =
    p.voices.find((v) => v.id === p.selectedVoiceId)?.display_name ?? p.selectedVoiceId
  const finished = st && !st.running && st.status === "done" && st.pages.length > 0
  const failedCount = st?.pages.filter((pg) => pg.failed).length ?? 0
  const totalDuration = useMemo(
    () => (st?.pages ?? []).reduce((acc, pg) => acc + (pg.failed ? 0 : pg.duration_s), 0),
    [st]
  )
  const [rawText, setRawText] = useState("")
  const fileInput = useRef<HTMLInputElement | null>(null)

  const downloadManifest = () => {
    if (!st) return
    const manifest = {
      job: st.job_dir,
      voice_id: st.voice_id,
      total_pages: st.pages.length,
      total_duration_s: Math.round(totalDuration * 100) / 100,
      pages: st.pages.map((pg) => ({
        page: pg.page,
        duration_s: pg.duration_s,
        failed: pg.failed,
        file: pg.failed ? "" : `page_${String(pg.page).padStart(3, "0")}.wav`,
      })),
    }
    const blob = new Blob([JSON.stringify(manifest, null, 2)], { type: "application/json" })
    const a = document.createElement("a")
    a.href = URL.createObjectURL(blob)
    a.download = `${st.job_dir || "course"}-manifest.json`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="text-xs font-medium text-primary">讲稿录课 · 逐页旁白</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            讲稿驱动录课
          </h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            把讲稿写进 PPT 每页的「备注」（或用 --- 分页的纯文本），选一个音色逐页生成旁白；
            每页一条独立音轨 + 时长清单，逐页对齐导出，插回课件即可。拟用于备课提效场景。
            想用自己的声音：先在「声音保存」录一段，回来在这里选它即可。
          </p>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-[minmax(0,1.6fr)_360px] lg:px-12 lg:py-14">
        <section className="space-y-8">
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="text-xs font-medium text-primary">第一步 · 导入讲稿</p>
                <h3 className="mt-2 text-lg font-semibold text-card-foreground">PPT 备注页 / 分页文本</h3>
              </div>
              {p.pages.length > 0 && (
                <span className="rounded-full bg-primary/15 px-2.5 py-0.5 text-xs text-primary">
                  {p.source === "pptx" ? `PPT · ${p.pages.length} 页有备注` : `文本 · ${p.pages.length} 页`}
                </span>
              )}
            </div>

            <div className="mt-5 flex flex-wrap items-center gap-3">
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-3 py-2 text-xs font-medium text-muted-foreground transition hover:border-primary hover:text-primary">
                <FileUp className="h-3.5 w-3.5" />上传 .pptx / .txt
                <input
                  ref={fileInput}
                  type="file"
                  accept=".pptx,.txt"
                  className="hidden"
                  onChange={(e) => {
                    p.loadFile(e.target.files?.[0])
                    if (e.target.files?.[0]?.name.toLowerCase().endsWith(".pptx")) setRawText("")
                    e.target.value = ""
                  }}
                />
              </label>
              {p.fileName && <span className="font-mono text-xs text-muted-foreground">{p.fileName}</span>}
              {p.parsing && <Loader2 className="h-4 w-4 animate-spin text-primary" />}
            </div>

            <textarea
              value={rawText}
              onChange={(e) => setRawText(e.target.value)}
              rows={6}
              placeholder={"没有 PPT？把讲稿贴到这里，用单独一行 --- 分页：\n第一页：同学们好，今天讲第三章。\n---\n第二页：先看一个例子。"}
              className="mt-4 w-full resize-y rounded-md border border-border bg-background px-3 py-2.5 font-mono text-sm leading-6 text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
            />
            <button
              type="button"
              disabled={p.parsing || p.running || !rawText.trim()}
              onClick={() => void p.loadText(rawText)}
              className="mt-2 inline-flex items-center gap-2 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
            >
              解析这段文本
            </button>

            {p.skipped > 0 && (
              <p className="mt-3 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-600">
                {p.skipped} 页没有备注，已跳过 —— 需要它们发声的话，把讲稿补进对应页的「备注」再重新上传。
              </p>
            )}

            <div className="mt-6 border-t border-border pt-5">
              <p className="text-xs font-medium text-primary">第二步 · 选音色并合成</p>
              <label htmlFor="course-voice" className="mt-3 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                <Presentation className="h-3.5 w-3.5" />旁白音色
              </label>
              {p.voices.length ? (
                <select
                  id="course-voice"
                  value={p.selectedVoiceId ?? ""}
                  onChange={(e) => p.selectVoice(e.target.value)}
                  className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  {p.voices.map((v) => (
                    <option key={v.id} value={v.id}>{voiceOptionLabel(v)}</option>
                  ))}
                </select>
              ) : (
                <div className="mt-2 flex items-center justify-between gap-3 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
                  <span>还没有可用音色。</span>
                  <Link to="/voices" className="shrink-0 font-medium underline underline-offset-2">去音色库挖掘 →</Link>
                </div>
              )}
              {selectedName && <p className="mt-2 text-xs text-muted-foreground">当前音色：{selectedName}</p>}

              <div className="mt-4">
                <label htmlFor="course-gap" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>句间停顿</span>
                  <span className="font-mono text-primary">{p.gapMs} ms</span>
                </label>
                <input
                  id="course-gap"
                  type="range"
                  min={0}
                  max={1500}
                  step={50}
                  value={p.gapMs}
                  onChange={(e) => p.setGapMs(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]"
                />
              </div>
            </div>

            {!p.backendUp && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
                <CircleAlert className="h-4 w-4" />本地服务离线，请确认后端已启动。
              </div>
            )}
            {p.errorMessage && <ErrorPanel title="讲稿录课失败" detail={p.errorMessage} />}
            {st?.error && <ErrorPanel title="合成异常" detail={st.error} />}

            {st?.running && (
              <div className="mt-5 rounded-lg border border-border bg-background/60 p-4">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium text-foreground">
                    逐页合成 · {st.done}/{st.total} 页
                  </span>
                  <span className="font-mono text-primary">{st.percent}%</span>
                </div>
                <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
                  <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${st.percent}%` }} />
                </div>
                {st.current_page > 0 && (
                  <p className="mt-2 line-clamp-1 text-xs text-muted-foreground">
                    正在合成第 {st.current_page} 页：{st.current_text}
                  </p>
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
                {p.submitting ? "提交中…" : "逐页合成"}
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
              {finished && failedCount > 0 && (
                <button
                  type="button"
                  onClick={() => void p.retry()}
                  className="inline-flex items-center gap-2 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-4 py-3 text-sm font-medium text-yellow-600 transition hover:bg-yellow-500/20"
                >
                  <RefreshCw className="h-4 w-4" />重试失败页（{failedCount}）
                </button>
              )}
            </div>
          </div>
        </section>

        <section>
          <div className="sticky top-[116px] lg:top-24 rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs font-medium text-primary">逐页音轨</p>
                <h3 className="mt-2 flex items-center gap-2 text-xl font-semibold text-card-foreground">
                  <Presentation className="h-4 w-4 text-primary" />
                  {st?.pages.length ? `${st.pages.length} 页` : "待合成"}
                </h3>
              </div>
              {finished && (
                <button
                  type="button"
                  onClick={downloadManifest}
                  className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 text-xs font-medium text-primary transition hover:bg-primary/20"
                >
                  <Download className="h-3.5 w-3.5" />时长清单
                </button>
              )}
            </div>
            {finished && (
              <p className="mt-2 font-mono text-xs text-muted-foreground">
                合计 {totalDuration.toFixed(1)}s · 逐页 wav 在 outputs/{st?.job_dir}/
              </p>
            )}
            {st?.pages.length ? (
              <ul className="mt-5 max-h-[60vh] space-y-3 overflow-y-auto pr-1">
                {st.pages.map((pg) => (
                  <li
                    key={pg.page}
                    className={`rounded-lg border p-3 ${pg.failed ? "border-destructive/40 bg-destructive/5" : "border-border bg-background/60"}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-xs text-primary">第 {pg.page} 页</span>
                      <span className={`font-mono text-xs ${pg.failed ? "text-destructive" : "text-muted-foreground"}`}>
                        {pg.failed ? "合成失败" : `${pg.duration_s}s`}
                      </span>
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-card-foreground">{pg.text}</p>
                    {pg.failed ? (
                      <p className="mt-1 line-clamp-1 text-xs text-destructive/80">{pg.error}</p>
                    ) : (
                      pg.url && <StudioAudioPlayer src={mediaUrl(pg.url)} label={`第 ${pg.page} 页`} className="mt-2" />
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-5 text-xs leading-5 text-muted-foreground">
                导入讲稿并提交后，每页合成的独立音轨会出现在这里，可逐页试听检查；页号与课件页一一对应。
              </p>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}
