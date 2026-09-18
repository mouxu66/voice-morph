import {
  Circle,
  CircleStop,
  Download,
  FileUp,
  Loader2,
  Mic,
  Sparkles,
  X,
} from "lucide-react"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { ErrorPanel } from "@/components/ErrorPanel"
import type { useSeedVc } from "@/pages/SeedVc/useSeedVc"
import { voiceOptionLabel } from "@/lib/voiceLabel"

export function SeedVcPage(p: ReturnType<typeof useSeedVc>) {
  const st = p.status
  const done = st?.status === "done" && p.resultUrl
  const voiceName = p.voices.find((v) => v.id === p.voiceId)?.display_name ?? p.voiceId

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="text-xs font-medium text-primary">表现力变声</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">表达力变声（Seed-VC）</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            零样本换声：给一段目标参考音即可变声，无需训练。相比 RVC 只保音高，Seed-VC 能
            <span className="text-foreground"> 保留乃至转换语气、节奏与情绪</span>——适合对「像不像真人在说」要求高的配音。
          </p>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-[minmax(0,1.6fr)_360px] lg:px-12 lg:py-14">
        <section className="space-y-8">
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <p className="text-xs font-medium text-primary">第一步 · 准备音频</p>
            <h3 className="mt-2 text-lg font-semibold text-card-foreground">录音或导入要变声的音频</h3>

            <div className="mt-5 flex flex-wrap items-center gap-3">
              {p.recording ? (
                <button
                  type="button"
                  onClick={p.stopRecording}
                  className="inline-flex items-center gap-2 rounded-md bg-destructive px-5 py-3 text-sm font-medium text-destructive-foreground shadow-md transition hover:scale-105"
                >
                  <CircleStop className="h-4 w-4" />停止录音（{p.recordSeconds}s）
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => void p.startRecording()}
                  className="inline-flex items-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105"
                >
                  {p.recording ? <Circle className="h-4 w-4 animate-pulse" /> : <Mic className="h-4 w-4" />}{p.recording ? "录音中" : "开始录音"}
                </button>
              )}
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-4 py-3 text-sm font-medium text-muted-foreground transition hover:border-primary hover:text-primary">
                <FileUp className="h-4 w-4" />导入音频
                <input type="file" accept="audio/*,.wav,.mp3,.m4a,.webm,.flac" className="hidden"
                  onChange={(e) => { p.pickFile(e.target.files?.[0]); e.target.value = "" }} />
              </label>
            </div>

            {p.audioFile && (
              <div className="mt-4 flex items-center justify-between gap-3 rounded-md border border-border bg-background/60 px-3 py-2.5 text-xs">
                <span className="truncate text-card-foreground">{p.audioFile.name} · {(p.audioFile.size / 1024 / 1024).toFixed(2)} MB</span>
                <button type="button" onClick={p.clearFile} className="text-muted-foreground transition hover:text-destructive" aria-label="移除音频">
                  <X className="h-4 w-4" />
                </button>
              </div>
            )}

            <div className="mt-8 border-t border-border pt-6">
              <p className="text-xs font-medium text-primary">第二步 · 目标音色</p>
              <h3 className="mt-2 text-lg font-semibold text-card-foreground">零样本，给参考音即可</h3>

              <div className="mt-4 flex flex-wrap items-center gap-4 text-xs">
                <label className="inline-flex cursor-pointer items-center gap-2">
                  <input type="radio" checked={!p.useUpload} onChange={() => p.setUseUpload(false)}
                    className="h-4 w-4 accent-[var(--primary)]" />
                  用音色库音色
                </label>
                <label className="inline-flex cursor-pointer items-center gap-2">
                  <input type="radio" checked={p.useUpload} onChange={() => p.setUseUpload(true)}
                    className="h-4 w-4 accent-[var(--primary)]" />
                  上传参考音频
                </label>
              </div>

              {!p.useUpload ? (
                <div className="mt-4">
                  {p.voices.length ? (
                    <select value={p.voiceId} onChange={(e) => p.setVoiceId(e.target.value)}
                      className="w-full rounded-md border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary">
                      {p.voices.map((v) => (
                        <option key={v.id} value={v.id}>{voiceOptionLabel(v)}</option>
                      ))}
                    </select>
                  ) : (
                    <div className="rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
                      音色库还没有带参考音频的音色，可先到「发掘音色 / 音色微调」建一个，或改用上传参考音频。
                    </div>
                  )}
                  {voiceName && <p className="mt-2 text-xs text-muted-foreground">当前音色：{voiceName}（以其 reference.wav 为参考）</p>}
                </div>
              ) : (
                <div className="mt-4">
                  <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-4 py-2.5 text-sm font-medium text-muted-foreground transition hover:border-primary hover:text-primary">
                    <FileUp className="h-4 w-4" />选择参考音频（建议 5–25s）
                    <input type="file" accept="audio/*,.wav,.mp3,.m4a,.webm,.flac" className="hidden"
                      onChange={(e) => { p.pickTargetFile(e.target.files?.[0]); e.target.value = "" }} />
                  </label>
                  {p.targetFile && (
                    <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-border bg-background/60 px-3 py-2.5 text-xs">
                      <span className="truncate text-card-foreground">{p.targetFile.name} · {(p.targetFile.size / 1024 / 1024).toFixed(2)} MB</span>
                      <button type="button" onClick={p.clearTargetFile} className="text-muted-foreground transition hover:text-destructive" aria-label="移除参考音频">
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="mt-8 border-t border-border pt-6">
              <p className="text-xs font-medium text-primary">第三步 · 表达力</p>
              <h3 className="mt-2 text-lg font-semibold text-card-foreground">情绪与相似度</h3>

              <label className="mt-4 flex cursor-pointer items-start gap-2 text-sm text-card-foreground">
                <input type="checkbox" checked={p.convertStyle} onChange={(e) => p.setConvertStyle(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-[var(--primary)]" />
                <span>
                  转换语气/情绪（--convert-style）
                  <span className="ml-1 text-xs text-muted-foreground">（关闭则保留源音频自己的语气，只换音色）</span>
                </span>
              </label>

              <div className="mt-5">
                <label htmlFor="seedvc-sim" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>音色相似度（similarity_cfg_rate）</span>
                  <span className="font-mono text-primary">{p.sim.toFixed(2)}</span>
                </label>
                <input id="seedvc-sim" type="range" min={0} max={1} step={0.05} value={p.sim}
                  onChange={(e) => p.setSim(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]" />
                <p className="mt-1 text-xs leading-5 text-muted-foreground">越高越贴参考音色；过低会飘向陌生声音，0.7 起步。</p>
              </div>

              <div className="mt-5">
                <label htmlFor="seedvc-topp" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>生成多样性（top_p）</span>
                  <span className="font-mono text-primary">{p.topP.toFixed(2)}</span>
                </label>
                <input id="seedvc-topp" type="range" min={0.6} max={1} step={0.05} value={p.topP}
                  onChange={(e) => p.setTopP(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]" />
                <p className="mt-1 text-xs leading-5 text-muted-foreground">低于 0.6 会生成崩坏（静音/复读），实测 0.5 时整段报废，故下限锁 0.6。</p>
              </div>

              <div className="mt-5">
                <label htmlFor="seedvc-temp" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>temperature</span>
                  <span className="font-mono text-primary">{p.temperature.toFixed(2)}</span>
                </label>
                <input id="seedvc-temp" type="range" min={0} max={2} step={0.05} value={p.temperature}
                  onChange={(e) => p.setTemperature(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]" />
                <p className="mt-1 text-xs leading-5 text-muted-foreground">top_p / temperature 越高情绪越外放，过高会吐字不稳。</p>
              </div>

              <div className="mt-5 grid gap-5 sm:grid-cols-2">
                <div>
                  <label htmlFor="seedvc-steps" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                    <span>diffusion_steps</span>
                    <span className="font-mono text-primary">{p.steps}</span>
                  </label>
                  <input id="seedvc-steps" type="range" min={1} max={50} step={1} value={p.steps}
                    onChange={(e) => p.setSteps(Number(e.target.value))}
                    className="mt-2 w-full accent-[var(--primary)]" />
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">越多质量越好但更慢。</p>
                </div>
                <div>
                  <label htmlFor="seedvc-len" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                    <span>语速（length_adjust）</span>
                    <span className="font-mono text-primary">{p.lenAdjust.toFixed(2)}</span>
                  </label>
                  <input id="seedvc-len" type="range" min={0.5} max={2} step={0.05} value={p.lenAdjust}
                    onChange={(e) => p.setLenAdjust(Number(e.target.value))}
                    className="mt-2 w-full accent-[var(--primary)]" />
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">&lt;1 加速，&gt;1 减速，1 为原速。</p>
                </div>
              </div>

              <label className="mt-5 flex cursor-pointer items-center gap-2 text-sm text-card-foreground">
                <input type="checkbox" checked={p.denoise} onChange={(e) => p.setDenoise(e.target.checked)}
                  className="h-4 w-4 accent-[var(--primary)]" />
                输入降噪（推荐开着麦克风录音时使用）
              </label>
            </div>

            {p.errorMessage && (
              <ErrorPanel title="表达力变声操作失败" detail={p.errorMessage} />
            )}
            {st?.error && (
              <ErrorPanel title="表达力变声转换失败" detail={st.error} />
            )}
            {!p.errorMessage && !st?.error && st?.running && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2.5 text-xs text-primary">
                <Loader2 className="h-4 w-4 animate-spin" />{st.message || "处理中…"}
              </div>
            )}

            <button type="button" disabled={!p.canSubmit} onClick={() => void p.submit()}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50 sm:w-auto">
              {p.submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              {p.submitting ? "提交中…" : "开始表达力变声"}
            </button>
          </div>

          {done && st && (
            <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-xs font-medium text-primary">转换结果</p>
                  <h3 className="mt-2 text-lg font-semibold text-card-foreground">变声完成 · 全长 {st.duration_s}s</h3>
                </div>
                <a href={p.resultUrl} download={`seedvc-${st.target}.wav`}
                  className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20">
                  <Download className="h-4 w-4" />下载 wav
                </a>
              </div>
              <StudioAudioPlayer src={p.resultUrl} label="播放表达力变声结果" className="mt-4" />
            </div>
          )}
        </section>

        <section>
          <div className="sticky top-[116px] lg:top-24 rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6">
            <p className="text-xs font-medium text-primary">使用提示</p>
            <h3 className="mt-2 text-xl font-semibold text-card-foreground">小贴士</h3>
            <ul className="mt-5 space-y-3 text-xs leading-6 text-muted-foreground">
              <li className="rounded-lg border border-border bg-background/60 p-3">零样本：目标参考音 5–25s 即可，不需要像 RVC 那样先训练。</li>
              <li className="rounded-lg border border-border bg-background/60 p-3">「转换语气/情绪」开着会把输出往参考音的说话方式靠；只想换音色、保留自己语气就关掉。</li>
              <li className="rounded-lg border border-border bg-background/60 p-3">单次转换约 1 分钟内（视时长），离线非实时；要实时变声请用「实时变声」页（RVC）。</li>
              <li className="rounded-lg border border-border bg-background/60 p-3">与实时变声/级联共用显卡，它们运行时无法提交，请先停止。</li>
            </ul>
          </div>
        </section>
      </main>
    </div>
  )
}
