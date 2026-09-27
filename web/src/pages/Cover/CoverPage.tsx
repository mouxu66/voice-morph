import { useRef } from "react"
import { CircleAlert, Link2, Loader2, Music4, Sparkles, Upload, Wand2 } from "lucide-react"
import type { useCover } from "@/pages/Cover/useCover"
import { ErrorPanel } from "@/components/ErrorPanel"
import { PageShell } from "@/components/layout/PageShell"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { cn } from "@/lib/utils"

/** 工序 → 给用户看的话。只给百分比用户会以为卡死（demucs + RVC 要几分钟）。 */
const STEP_LABEL: Record<string, string> = {
  separate: "分离人声与伴奏",
  convert: "换音色",
  mix: "与伴奏合成",
}

export function CoverPage(p: ReturnType<typeof useCover>) {
  const inputRef = useRef<HTMLInputElement>(null)
  const s = p.status
  const percent = s?.percent ?? 0

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <PageShell className="space-y-8">
        <header>
          <p className="text-xs font-medium text-primary">翻唱</p>
          <h2 className="mt-2 text-2xl font-semibold text-foreground">把一首歌换成你的音色</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">
            上传一首歌，程序会先把人声和伴奏拆开，只把<strong className="text-card-foreground">人声</strong>换成你选的音色，
            再和原伴奏合回去。因为换声只改音色、不改音高曲线，所以原唱的
            <strong className="text-card-foreground">转音、颤音、换气、节奏全部保持不变</strong>
            ——唱功是原唱的，声音是你的。
          </p>
        </header>

        {p.errorMessage && <ErrorPanel title="翻唱失败" detail={p.errorMessage} />}
        {p.feedback && (
          <div className="flex items-center gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2.5 text-xs text-primary animate-in fade-in slide-in-from-top-2 duration-300">
            <CircleAlert className="h-3.5 w-3.5" />
            {p.feedback}
          </div>
        )}

        <section className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
          {/* 左：选歌 + 参数 */}
          <div className="space-y-5 rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div>
              <p className="text-sm font-semibold text-card-foreground">1. 选择歌曲</p>
              <div
                role="button"
                tabIndex={0}
                onClick={() => inputRef.current?.click()}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault()
                    inputRef.current?.click()
                  }
                }}
                className="mt-3 flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed border-border bg-background/50 px-4 py-8 text-center transition hover:border-primary/60"
              >
                <Music4 className="h-7 w-7 text-primary" />
                {p.file ? (
                  <>
                    <p className="mt-2 text-sm font-medium text-card-foreground">{p.file.name}</p>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {(p.file.size / 1024 / 1024).toFixed(1)} MB · 点击更换
                    </p>
                  </>
                ) : p.srcName ? (
                  <>
                    <p className="mt-2 text-sm font-medium text-card-foreground">已用直链下载的音频</p>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {p.srcSummary} · 点击改用本地文件
                    </p>
                  </>
                ) : (
                  <>
                    <p className="mt-2 text-sm text-muted-foreground">
                      点击选择歌曲文件
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">支持 mp3 / wav / m4a / flac / mp4 等</p>
                  </>
                )}
              </div>
              <input
                ref={inputRef}
                type="file"
                accept="audio/*,video/*,.mp3,.wav,.m4a,.flac,.aac,.ogg"
                className="hidden"
                onChange={(e) => p.pickFile(e.target.files?.[0] ?? null)}
              />

              {/* 粘直链：想到哪首下哪首，试听完再跑；退出即删，不占地方 */}
              <div className="mt-4 rounded-xl border border-border bg-background/40 p-3">
                <p className="text-xs font-medium text-card-foreground">或者：粘一条音频直链</p>
                <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                  公开的音频直链（.mp3 / .m4a / .flac / .wav 等）能直接下；网页链接不行（会明确告诉你）。
                  下载的歌只在本次会话里，退出即删 —— 开跑后也会被清掉。
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <input
                    type="url"
                    value={p.srcUrl}
                    onChange={(e) => p.setSrcUrl(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault()
                        void p.fetchFromUrl()
                      }
                    }}
                    placeholder="https://…/song.mp3"
                    disabled={p.running || p.fetching}
                    className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 font-mono text-xs text-card-foreground placeholder:text-muted-foreground/60 disabled:opacity-50"
                    aria-label="音频直链"
                  />
                  <button
                    type="button"
                    onClick={() => void p.fetchFromUrl()}
                    disabled={p.running || p.fetching || !p.srcUrl.trim()}
                    className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20 disabled:pointer-events-none disabled:opacity-40"
                  >
                    {p.fetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Link2 className="h-3.5 w-3.5" />}
                    {p.fetching ? "下载中…" : "下载并试听"}
                  </button>
                </div>
                {p.srcPreview && (
                  <div className="mt-2 rounded-lg border border-primary/30 bg-primary/5 p-2">
                    <div className="flex items-center justify-between gap-2 px-1 text-[11px] text-muted-foreground">
                      <span className="truncate">已下载：{p.srcSummary || "音频"}</span>
                      <button
                        type="button"
                        onClick={p.clearSource}
                        className="shrink-0 underline-offset-2 hover:text-foreground hover:underline"
                      >
                        丢弃
                      </button>
                    </div>
                    <div className="mt-1.5">
                      <StudioAudioPlayer src={p.srcPreview} />
                    </div>
                    <p className="mt-1.5 px-1 text-[11px] leading-4 text-muted-foreground">
                      这是原曲（还没换声）。确认没错再点右边「开始翻唱」。
                    </p>
                  </div>
                )}
              </div>
            </div>

            <div>
              <p className="text-sm font-semibold text-card-foreground">2. 变调</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                男女声互转通常要 ±12 半音。不知道填多少就点右边的「自动建议」——
                程序会分离出人声、量它的音域中心，和你选的角色对比后给一个值。
              </p>
              <div className="mt-3 flex items-center gap-3">
                <input
                  type="range"
                  min={-24}
                  max={24}
                  step={1}
                  value={p.pitch}
                  disabled={p.autoPitch}
                  onChange={(e) => p.setPitch(Number(e.target.value))}
                  className="h-1.5 flex-1 accent-primary disabled:opacity-40"
                  aria-label="变调半音数"
                />
                <span className="w-16 shrink-0 text-right font-mono text-sm text-card-foreground">
                  {p.pitch > 0 ? "+" : ""}
                  {p.pitch}
                </span>
              </div>
              <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={p.autoPitch}
                  onChange={(e) => p.setAutoPitch(e.target.checked)}
                  className="accent-primary"
                />
                开跑时自动算建议变调（勾上就忽略上面的滑块）
              </label>
            </div>

            <div>
              <p className="text-sm font-semibold text-card-foreground">3. 音量平衡</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                自动配平：按分离后的实测音量，把 RVC 换声输出的人声拉到和伴奏相称的比例
                （换完的人声普遍偏小，这是最常见的"听不清"）。关掉后才用下面两个滑块手动调。
              </p>
              <label className="mt-2 flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={p.autoGain}
                  onChange={(e) => p.setAutoGain(e.target.checked)}
                  className="accent-primary"
                />
                自动配平（默认开；勾上时下面两个滑块不生效）
              </label>
              <div className={cn("mt-3 space-y-2.5", p.autoGain && "pointer-events-none opacity-40")}>
                <GainRow label="人声" value={p.vocalGain} onChange={p.setVocalGain} />
                <GainRow label="伴奏" value={p.accompGain} onChange={p.setAccompGain} />
              </div>
            </div>
          </div>

          {/* 右：音色 + 开跑 + 结果 */}
          <aside className="space-y-5">
            <div className="rounded-2xl border border-border bg-card p-5 shadow-lg">
              <p className="text-sm font-semibold text-card-foreground">音色</p>
              {p.voices.length === 0 ? (
                <p className="mt-3 rounded-lg border border-dashed border-border bg-background/40 px-3 py-2 text-xs text-muted-foreground">
                  还没有已训练的音色。先去「训练变声」做出一个，再回来翻唱。
                </p>
              ) : (
                <select
                  value={p.voiceId}
                  onChange={(e) => p.setVoiceId(e.target.value)}
                  disabled={p.running}
                  className="mt-3 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-card-foreground disabled:opacity-50"
                >
                  {p.voices.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.display_name || v.id}
                    </option>
                  ))}
                </select>
              )}

              <button
                type="button"
                onClick={() => void p.analyzePitch()}
                disabled={!p.hasSource || !p.voiceId || p.running || p.analyzing}
                className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20 disabled:pointer-events-none disabled:opacity-40"
              >
                {p.analyzing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
                {p.analyzing ? "分析音域中…（约半分钟）" : "自动建议变调"}
              </button>

              <button
                type="button"
                onClick={() => void p.start()}
                disabled={!p.hasSource || !p.voiceId || p.running}
                className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-[1.02] disabled:pointer-events-none disabled:opacity-50"
              >
                {p.running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                {p.running ? "处理中" : "开始翻唱"}
              </button>

              {p.running && (
                <div className="mt-4 rounded-lg border border-primary/30 bg-primary/5 px-3 py-3">
                  <div className="flex items-center justify-between gap-2 text-xs text-primary">
                    <span className="truncate">
                      {s?.step ? `${STEP_LABEL[s.step] ?? s.step}` : "处理中"}
                    </span>
                    <span className="shrink-0 font-mono">{Math.round(percent)}%</span>
                  </div>
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{s?.message || "正在启动…"}</p>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-primary/20">
                    <div
                      className="h-full rounded-full bg-primary transition-[width] duration-500"
                      style={{ width: `${Math.max(2, percent)}%` }}
                    />
                  </div>
                  <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
                    整首歌要几分钟（分离 + 换声），别关页面。
                  </p>
                </div>
              )}
            </div>

            {s?.status === "done" && p.resultUrl && (
              <div className="rounded-2xl border border-emerald-500/40 bg-emerald-500/10 p-5 shadow-lg">
                <p className="text-sm font-semibold text-emerald-600">翻唱完成</p>
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {s.duration_s.toFixed(1)} 秒 · 变调 {s.pitch > 0 ? "+" : ""}
                  {s.pitch} 半音
                  {/* 自动配平时的实测人声增益。不显示就等于暗箱：
                      滑块还停在用户拖的值上，真正生效的却是这个数（2026-09-27 修）。 */}
                  {s.vocal_gain_applied > 0 &&
                    ` · 人声 ×${s.vocal_gain_applied.toFixed(2)}${
                      Math.abs(s.vocal_gain_applied - p.vocalGain) > 0.005 ? "（自动配平）" : ""
                    }`}
                </p>
                <div className="mt-3">
                  <StudioAudioPlayer src={p.resultUrl} />
                </div>
                <a
                  href={p.resultUrl}
                  download
                  className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-md border border-emerald-500/40 bg-background/40 px-3 py-2 text-xs font-medium text-emerald-600 transition hover:bg-emerald-500/10"
                >
                  <Upload className="h-3.5 w-3.5 rotate-180" />
                  下载成品
                </a>
                <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
                  成品只在本次会话里，退出应用会自动清掉 —— 要留就先下载。
                </p>
              </div>
            )}

            {s?.status === "error" && s.error && <ErrorPanel title="翻唱中断" detail={s.error} />}
          </aside>
        </section>
      </PageShell>
    </div>
  )
}

function GainRow({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className="flex items-center gap-3">
      <span className="w-10 shrink-0 text-xs text-muted-foreground">{label}</span>
      <input
        type="range"
        min={0}
        max={2}
        step={0.05}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1.5 flex-1 accent-primary"
        aria-label={`${label}音量`}
      />
      <span className="w-12 shrink-0 text-right font-mono text-xs text-card-foreground">{value.toFixed(2)}</span>
    </div>
  )
}
