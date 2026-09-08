import {
  AudioLines,
  CheckCircle2,
  Circle,
  CircleAlert,
  CircleStop,
  Download,
  FileUp,
  Loader2,
  Mic,
  Sparkles,
  X,
} from "lucide-react"
import { useState } from "react"
import { Link } from "react-router-dom"
import { mediaUrl } from "@/api/client"
import { downloadUrl } from "@/lib/download"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { ErrorPanel } from "@/components/ErrorPanel"
import type { useOfflineVc } from "@/pages/OfflineVc/useOfflineVc"
import { voiceOptionLabel } from "@/lib/voiceLabel"

const PITCH_PRESETS: [string, number][] = [
  ["原调", 0], ["男→女 +12", 12], ["女→男 -12", -12], ["+5 清亮", 5], ["-5 低沉", -5],
]

export function OfflineVcPage(p: ReturnType<typeof useOfflineVc>) {
  const st = p.status
  const done = st?.status === "done" && p.resultUrl
  const voiceName = p.rvcVoices.find((v) => v.id === p.voiceId)?.display_name ?? p.voiceId
  const [presetNameOpen, setPresetNameOpen] = useState(false)
  const [presetName, setPresetName] = useState("")
  const [activePresetId, setActivePresetId] = useState("")
  // 下载状态：失败的 key+原因必须浮出到 UI，不再静默吞掉
  const [dlBusy, setDlBusy] = useState<string | null>(null)
  const [dlFail, setDlFail] = useState<{ key: string; msg: string } | null>(null)

  const runDownload = async (key: string, url: string, filename: string) => {
    setDlFail(null)
    setDlBusy(key)
    try {
      await downloadUrl(url, filename)
    } catch (e) {
      setDlFail({ key, msg: e instanceof Error ? e.message : "下载失败" })
    } finally {
      setDlBusy(null)
    }
  }

  const confirmSavePreset = () => {
    if (!presetName.trim()) return
    p.savePreset(presetName)
    setPresetName("")
    setPresetNameOpen(false)
  }

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">STAGE 06 / OFFLINE VOICE CHANGE</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">离线变声工作台</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            录一段音或导入音频文件，选好目标音色，整段离线转换后导出 48kHz wav——质量比实时变声更稳，适合后期配音。
          </p>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-[minmax(0,1.6fr)_360px] lg:px-12 lg:py-14">
        <section className="space-y-8">
          <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">第一步 · 准备音频</p>
            <h3 className="mt-2 text-lg font-semibold text-card-foreground">录音或导入文件</h3>

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
                <FileUp className="h-4 w-4" />导入音频（可多选）
                <input type="file" accept="audio/*,.wav,.mp3,.m4a,.webm,.flac" multiple className="hidden"
                  onChange={(e) => { p.importFiles(Array.from(e.target.files ?? [])); e.target.value = "" }} />
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
              <p className="font-mono text-xs uppercase tracking-widest text-primary">第二步 · 转换设置</p>
              <h3 className="mt-2 text-lg font-semibold text-card-foreground">目标音色与参数</h3>

              <div className="mt-4 flex flex-wrap items-center gap-2 rounded-md border border-border bg-background/50 px-3 py-2.5">
                <span className="text-xs font-medium text-muted-foreground">参数预设</span>
                {p.presets.length ? (
                  <select
                    value={activePresetId}
                    onChange={(e) => { setActivePresetId(e.target.value); if (e.target.value) p.applyPreset(e.target.value) }}
                    className="rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
                  >
                    <option value="">选择预设套用…</option>
                    {p.presets.map((pr) => (
                      <option key={pr.id} value={pr.id}>{pr.name}</option>
                    ))}
                  </select>
                ) : (
                  <span className="text-xs text-muted-foreground">暂无，调好参数后点「保存当前参数」</span>
                )}
                {activePresetId && (
                  <button type="button" onClick={() => { p.deletePreset(activePresetId); setActivePresetId("") }}
                    className="text-xs text-muted-foreground transition hover:text-destructive">删除该预设</button>
                )}
                <span className="flex-1" />
                {presetNameOpen ? (
                  <>
                    <input autoFocus value={presetName} onChange={(e) => setPresetName(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") confirmSavePreset() }}
                      placeholder="预设名称，如：萌妹+12"
                      className="w-40 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary" />
                    <button type="button" onClick={confirmSavePreset}
                      className="rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground transition hover:scale-105">保存</button>
                    <button type="button" onClick={() => { setPresetNameOpen(false); setPresetName("") }}
                      className="text-xs text-muted-foreground transition hover:text-foreground">取消</button>
                  </>
                ) : (
                  <button type="button" onClick={() => setPresetNameOpen(true)}
                    className="rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary">保存当前参数</button>
                )}
              </div>

              <div className="mt-5">
                <label htmlFor="ovc-voice" className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
                  <AudioLines className="h-3.5 w-3.5" />目标音色（需已训练 RVC 模型）
                </label>
                {p.rvcVoices.length ? (
                  <select id="ovc-voice" value={p.voiceId} onChange={(e) => p.setVoiceId(e.target.value)}
                    className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary">
                    {p.rvcVoices.map((v) => (
                      <option key={v.id} value={v.id}>{voiceOptionLabel(v)}</option>
                    ))}
                  </select>
                ) : (
                  <div className="mt-2 flex items-center justify-between gap-3 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
                    <span>还没有训练好的 RVC 模型。</span>
                    <Link to="/live" className="shrink-0 font-medium underline underline-offset-2">去实时变声页训练 →</Link>
                  </div>
                )}
                {voiceName && <p className="mt-2 text-xs text-muted-foreground">当前音色：{voiceName}</p>}
              </div>

              <div className="mt-5">
                <label htmlFor="ovc-pitch" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>变调（半音）</span>
                  <span className="font-mono text-primary">{p.pitch > 0 ? `+${p.pitch}` : p.pitch}</span>
                </label>
                <input id="ovc-pitch" type="range" min={-12} max={12} step={1} value={p.pitch}
                  onChange={(e) => p.setPitch(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]" />
                <div className="mt-2 flex flex-wrap gap-2">
                  {PITCH_PRESETS.map(([label, v]) => (
                    <button key={label} type="button" onClick={() => p.setPitch(v)}
                      className={`rounded-full px-2.5 py-1 text-xs transition ${p.pitch === v ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:text-foreground"}`}>
                      {label}
                    </button>
                  ))}
                </div>
                {p.pitchBusy && (
                  <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Loader2 className="h-3 w-3 animate-spin" />正在分析音频音高，稍后自动填入建议变调…
                  </p>
                )}
                {!p.pitchBusy && p.pitchAdvice && (
                  p.pitchAdvice.reliable && p.pitchAdvice.suggested_pitch != null && p.pitchAdvice.ref_f0 ? (
                    <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-xs text-primary">
                      <span>
                        已按音高自动建议变调：你 {p.pitchAdvice.input_f0}Hz → {voiceName || "目标音色"} {p.pitchAdvice.ref_f0}Hz
                      </span>
                      {p.pitch !== p.pitchAdvice.suggested_pitch && (
                        <button type="button"
                          onClick={() => p.setPitch(p.pitchAdvice!.suggested_pitch!)}
                          className="rounded-full bg-primary px-2 py-0.5 font-medium text-primary-foreground transition hover:scale-105">
                          填入建议 ({p.pitchAdvice.suggested_pitch > 0 ? `+${p.pitchAdvice.suggested_pitch}` : p.pitchAdvice.suggested_pitch})
                        </button>
                      )}
                    </div>
                  ) : (
                    <p className="mt-2 text-xs text-yellow-600">
                      音频里有效语音太少（{Math.round(p.pitchAdvice.voiced_ratio * 100)}%），音高建议不可靠——建议换段干净录音。
                    </p>
                  )
                )}
              </div>

              <div className="mt-5">
                <label htmlFor="ovc-index" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                  <span>音色检索强度（index rate）</span>
                  <span className="font-mono text-primary">{p.indexRate.toFixed(2)}</span>
                </label>
                <input id="ovc-index" type="range" min={0} max={1} step={0.05} value={p.indexRate}
                  onChange={(e) => p.setIndexRate(Number(e.target.value))}
                  className="mt-2 w-full accent-[var(--primary)]" />
                <p className="mt-1 text-xs leading-5 text-muted-foreground">越高越贴目标音色，过高会有训练腔；0.5 左右通常最自然。</p>
              </div>

              <div className="mt-5">
                <label className="flex cursor-pointer items-center gap-2 text-sm text-card-foreground">
                  <input type="checkbox" checked={p.denoise} onChange={(e) => p.setDenoise(e.target.checked)}
                    className="h-4 w-4 accent-[var(--primary)]" />
                  输入降噪（推荐开着麦克风录音时使用）
                </label>
                {p.denoise && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 pl-6 text-xs">
                    <span className="text-muted-foreground">降噪强度</span>
                    {([["light", "轻 · 保弱声"], ["standard", "标准"], ["strong", "强力"]] as const).map(([v, label]) => (
                      <button key={v} type="button" onClick={() => p.setEnhanceLevel(v)}
                        className={`rounded-full px-2.5 py-1 text-xs transition ${p.enhanceLevel === v ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:text-foreground"}`}>
                        {label}
                      </button>
                    ))}
                    <span className="text-muted-foreground">
                      {p.enhanceLevel === "light" && "最多压 6dB：弱人声/远场说话更安全，噪声残留多"}
                      {p.enhanceLevel === "standard" && "最多压 12dB：日常录音推荐"}
                      {p.enhanceLevel === "strong" && "不限压制：最干净，但弱人声易被压断（原默认行为）"}
                    </span>
                  </div>
                )}
              </div>

              <div className="mt-3">
                <p className="text-sm font-medium text-card-foreground">语气来源</p>
                <div className="mt-1.5 flex flex-col gap-1.5">
                  <label className="flex cursor-pointer items-start gap-2 text-sm text-card-foreground">
                    <input type="radio" name="prosody" checked={p.prosody === "keep"}
                      onChange={() => p.setProsody("keep")}
                      className="mt-0.5 h-4 w-4 accent-[var(--primary)]" />
                    <span>
                      跟随我的语气
                      <span className="ml-1 text-xs text-muted-foreground">（保留你说话的抑扬顿挫，口音与口头禅也会一起带进去）</span>
                    </span>
                  </label>
                  <label className="flex cursor-pointer items-start gap-2 text-sm text-card-foreground">
                    <input type="radio" name="prosody" checked={p.prosody === "relay"}
                      onChange={() => p.setProsody("relay")}
                      className="mt-0.5 h-4 w-4 accent-[var(--primary)]" />
                    <span>
                      重铸语气（文字中转）
                      <span className="ml-1 text-xs text-muted-foreground">（先转文字，再用目标音色的腔调重新合成一遍才变声：口音与口头禅被清掉，额外约 10~30 秒）</span>
                    </span>
                  </label>
                </div>
              </div>

              <label className="mt-3 flex cursor-pointer items-start gap-2 text-sm text-card-foreground">
                <input type="checkbox" checked={p.postSeedVc} onChange={(e) => p.setPostSeedVc(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-[var(--primary)]" />
                <span>
                  RVC 转完再用 Seed-VC 补情绪/韵律
                  <span className="ml-1 text-xs text-muted-foreground">（RVC 只保音高易压平语气；此开关用 Seed-VC 按参考音重塑情绪，额外约 1 分钟）</span>
                </span>
              </label>
            </div>

            {p.errorMessage && (
              <ErrorPanel title="离线变声操作失败" detail={p.errorMessage} />
            )}
            {st?.error && (
              <ErrorPanel title="离线变声转换失败" detail={st.error} />
            )}
            {!p.errorMessage && !st?.error && st?.running && (
              <div className="mt-5 flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2.5 text-xs text-primary">
                <Loader2 className="h-4 w-4 animate-spin" />{st.message || "处理中…"}
              </div>
            )}

            <button type="button" disabled={!p.canSubmit} onClick={() => void p.submit()}
              className="mt-6 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-5 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50 sm:w-auto">
              {p.submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              {p.submitting ? "提交中…" : "开始离线变声"}
            </button>
          </div>

          {p.queue.length > 0 && (
            <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="font-mono text-xs uppercase tracking-widest text-primary">批量队列</p>
                  <h3 className="mt-2 text-lg font-semibold text-card-foreground">
                    {p.queue.filter((it) => it.status === "done").length}/{p.queue.length} 已完成
                    <span className="ml-2 text-xs font-normal text-muted-foreground">统一使用上方音色与参数，逐个转换</span>
                  </h3>
                </div>
                <div className="flex items-center gap-2">
                  <button type="button" disabled={!p.canBatch} onClick={() => void p.startBatch()}
                    className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-xs font-medium text-primary-foreground shadow-md transition hover:scale-105 disabled:pointer-events-none disabled:opacity-50">
                    {p.batchProcessing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                    {p.batchProcessing ? "批量转换中…" : "开始批量转换"}
                  </button>
                  <button type="button" disabled={p.batchProcessing} onClick={p.clearQueue}
                    className="rounded-md border border-border bg-background px-3 py-2.5 text-xs text-muted-foreground transition hover:text-destructive disabled:opacity-50">
                    清空队列
                  </button>
                </div>
              </div>
              <ul className="mt-4 space-y-2">
                {p.queue.map((it) => (
                  <li key={it.id} className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-background/60 px-3 py-2.5 text-xs">
                    <span className="flex min-w-0 items-center gap-2">
                      {it.status === "done" ? (
                        <CheckCircle2 className="h-4 w-4 shrink-0 text-green-500" />
                      ) : it.status === "running" ? (
                        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary" />
                      ) : it.status === "error" ? (
                        <CircleAlert className="h-4 w-4 shrink-0 text-destructive" />
                      ) : (
                        <Circle className="h-4 w-4 shrink-0 text-muted-foreground" />
                      )}
                      <span className="truncate text-card-foreground" title={it.name}>{it.name}</span>
                      <span className="shrink-0 text-muted-foreground">· {(it.size / 1024 / 1024).toFixed(2)} MB</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-3">
                      {it.status === "done" && it.url ? (
                        <>
                          <span className="text-muted-foreground">{it.durationS ? `${it.durationS}s` : ""}</span>
                          <a href={mediaUrl(it.url)} download={`vc-${it.name.replace(/\.[^.]+$/, "")}.wav`}
                            onClick={(e) => { e.preventDefault(); const u = it.url; if (u) void runDownload(`q-${it.id}`, mediaUrl(u), `vc-${it.name.replace(/\.[^.]+$/, "")}.wav`) }}
                            className={`inline-flex items-center gap-1.5 font-medium text-primary transition hover:underline ${dlBusy === `q-${it.id}` ? "pointer-events-none opacity-60" : ""}`}>
                            {dlBusy === `q-${it.id}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                            {dlBusy === `q-${it.id}` ? "下载中…" : "下载 wav"}
                          </a>
                          {dlFail?.key === `q-${it.id}` && (
                            <span className="text-destructive" title={dlFail.msg}>下载失败：{dlFail.msg}</span>
                          )}
                        </>
                      ) : it.status === "error" ? (
                        <span className="rounded border border-destructive/40 bg-destructive/10 px-1.5 py-0.5 text-destructive">{it.error || "失败"}</span>
                      ) : it.status === "running" ? (
                        <span className="text-primary">转换中…</span>
                      ) : (
                        <span className="text-muted-foreground">排队中</span>
                      )}
                      {it.status !== "running" && (
                        <button type="button" onClick={() => p.removeQueueItem(it.id)} className="text-muted-foreground transition hover:text-destructive" aria-label="移除">
                          <X className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {done && st && (
            <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="font-mono text-xs uppercase tracking-widest text-primary">转换结果</p>
                  <h3 className="mt-2 text-lg font-semibold text-card-foreground">变声完成 · 全长 {st.duration_s}s</h3>
                </div>
                <a href={p.resultUrl} download={`offlinevc-${st.voice_id}.wav`}
                  onClick={(e) => { e.preventDefault(); void runDownload("result", p.resultUrl, `offlinevc-${st.voice_id}.wav`) }}
                  className={`inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20 ${dlBusy === "result" ? "pointer-events-none opacity-60" : ""}`}>
                  {dlBusy === "result" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                  {dlBusy === "result" ? "下载中…" : "下载 48kHz wav"}
                </a>
              </div>
              {dlFail?.key === "result" && (
                <p className="mt-2 text-xs text-destructive">下载失败：{dlFail.msg}</p>
              )}
              <StudioAudioPlayer src={p.resultUrl} label="播放变声结果" className="mt-4" />
            </div>
          )}
        </section>

        <section>
          <div className="sticky top-24 rounded-2xl border border-border bg-card p-5 shadow-lg sm:p-6">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">使用提示</p>
            <h3 className="mt-2 text-xl font-semibold text-card-foreground">小贴士</h3>
            <ul className="mt-5 space-y-3 text-xs leading-6 text-muted-foreground">
              <li className="rounded-lg border border-border bg-background/60 p-3">录音时离麦克风 20–30cm，环境噪音大就开着「输入降噪」。</li>
              <li className="rounded-lg border border-border bg-background/60 p-3">跨性别变调：男→女一般 +12 起，可微调 ±1 试听对比；同性别内容保持 0。</li>
              <li className="rounded-lg border border-border bg-background/60 p-3">离线转换与实时变声共用显卡，实时变声运行时无法提交，请先停止。</li>
              <li className="rounded-lg border border-border bg-background/60 p-3">整段单次推理，长音频（几分钟）大约需要 1–3 分钟，请耐心等待。</li>
            </ul>
          </div>
        </section>
      </main>
    </div>
  )
}
