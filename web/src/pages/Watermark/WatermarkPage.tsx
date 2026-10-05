import { useRef } from "react"
import {
  Check,
  CircleAlert,
  Download,
  Fingerprint,
  Loader2,
  ScanSearch,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react"
import type { useWatermark } from "@/pages/Watermark/useWatermark"
import { ErrorPanel } from "@/components/ErrorPanel"
import { Card, PageShell, Section } from "@/components/layout/PageShell"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { cn } from "@/lib/utils"
import { mediaUrl } from "@/api/client"

/**
 * 「音频水印」页 —— 三个区块，对应三个真实动作：
 * ① 权重就位（AudioSeal 模型 ~90MB，首次用时下载一次）；
 * ② 给一段音频嵌溯源码（4 位 hex，随机或指定）→ 会话产物，可试听可下载；
 * ③ 检测一段音频带没带我们的码（概率 + 码）。
 * 另有一枚「合成自动加标识」开关：开着时所有合成产物（含微信语音）自动嵌码。
 *
 * ★ 口径红线（文案与后端同一条铁律）：只说「可检出 / 可溯源」，
 *   不说「防伪 / 无法去除」—— 隐形水印是概率性取证信号，不是加密签名。
 */

const CODE_RE = /^[0-9a-fA-F]{4}$/

/** 字节进度 → 人话（total 未知时不显示百分比，别显示成 NaN%）。 */
function dlLabel(t: { status?: string; done?: number; total?: number | null }): string {
  if (t.status === "done") return "完成"
  if (t.status === "failed") return "失败"
  if (t.status === "cancelled") return "已取消"
  if (t.status === "downloading" && t.total) {
    const pct = Math.floor(((t.done ?? 0) / t.total) * 100)
    return `下载中 ${pct}%`
  }
  if (t.status === "downloading") return "下载中…"
  return "未开始"
}

export function WatermarkPage(p: ReturnType<typeof useWatermark>) {
  const embedInputRef = useRef<HTMLInputElement>(null)
  const detectInputRef = useRef<HTMLInputElement>(null)
  const st = p.status
  const installed = st?.installed ?? false
  const dlBusy =
    st?.download.generator?.status === "downloading" ||
    st?.download.detector?.status === "downloading"
  const dlFailed =
    st?.download.generator?.status === "failed" || st?.download.detector?.status === "failed"

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <PageShell className="space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <p className="mb-1.5 flex items-center gap-2 text-xs font-medium text-primary">
              <span className="h-3 w-0.5 rounded-full bg-primary/70" aria-hidden="true" />
              音频水印
            </p>
            <h1 className="text-xl font-semibold tracking-tight text-foreground">
              给音频嵌一个隐形的溯源码
            </h1>
            <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted-foreground">
              听感不变、时长不变，之后能<strong className="font-medium text-card-foreground">检出</strong>
              这段音频带没带你的码。适合给对外分享的语音留一个可溯源的标记 ——
              它是取证信号，不是加密防伪。
            </p>
          </div>
        </header>

        {p.error && <ErrorPanel title="操作失败" detail={p.error} />}
        {p.feedback && (
          <div className="flex items-center gap-2 rounded-xl border border-primary/30 bg-primary/5 px-3.5 py-2.5 text-xs text-primary animate-in fade-in slide-in-from-top-2 duration-300">
            <Check className="h-3.5 w-3.5 shrink-0" />
            {p.feedback}
          </div>
        )}

        {/* ① 权重状态：没装好时下面全是死路，先说清楚 */}
        <Section title="检测模型" desc="Meta AudioSeal（MIT 开源），首次使用下载一次即可，之后离线可用。">
          <Card className="p-5 sm:p-6">
            {p.loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> 读取状态中…
              </div>
            ) : installed ? (
              <div className="flex items-center gap-2.5 text-sm text-card-foreground">
                <ShieldCheck className="h-5 w-5 text-primary" />
                模型已就绪，可以开始使用（本机运行，不上传任何音频）。
              </div>
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
                  {dlFailed ? (
                    <TriangleAlert className="h-5 w-5 text-destructive" />
                  ) : (
                    <Download className={cn("h-5 w-5 text-primary", dlBusy && "animate-pulse")} />
                  )}
                  <span className="text-card-foreground">
                    权重未下载（共约 {st?.size_hint_mb ?? 90}MB，两个文件）
                  </span>
                  <button
                    type="button"
                    onClick={() => void p.startDownload()}
                    disabled={p.starting || dlBusy}
                    className={cn(
                      "ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2",
                      "text-xs font-medium text-primary-foreground transition hover:opacity-90",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    {dlBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                    {dlBusy ? "下载中" : p.starting ? "启动中…" : "下载权重"}
                  </button>
                </div>
                {st && (
                  <div className="grid gap-1.5 font-mono text-[11px] text-muted-foreground">
                    <ProgressLine label="generator" t={st.download.generator} />
                    <ProgressLine label="detector" t={st.download.detector} />
                  </div>
                )}
              </div>
            )}
          </Card>
        </Section>

        <div className={cn(!installed && "pointer-events-none opacity-50")}>
          <Section
            title="植入溯源码"
            desc="选一段音频（wav/mp3 等常见格式都行），嵌一个 4 位十六进制的码；留空则随机生成。结果只留在本次会话，退出即删。"
          >
            <Card className="p-5 sm:p-6">
              <div className="flex flex-col gap-3">
                <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
                  <input
                    ref={embedInputRef}
                    type="file"
                    accept="audio/*,.wav,.mp3,.flac,.ogg,.m4a,.aac,.opus,.webm"
                    onChange={(e) => p.changeEmbedFile(e.target.files?.[0] ?? null)}
                    className="hidden"
                  />
                  <button
                    type="button"
                    onClick={() => embedInputRef.current?.click()}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3.5 py-2",
                      "text-xs text-card-foreground transition hover:border-primary",
                    )}
                  >
                    <Fingerprint className="h-3.5 w-3.5 text-primary" />
                    {p.embedFile ? p.embedFile.name : "选择音频文件…"}
                  </button>
                  <input
                    value={p.code}
                    onChange={(e) => p.setCode(e.target.value)}
                    placeholder="溯源码（4 位 hex，留空随机）"
                    maxLength={4}
                    className={cn(
                      "w-full rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs",
                      "text-card-foreground placeholder:font-sans placeholder:text-muted-foreground/60",
                      "transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 sm:w-56",
                      p.code && !CODE_RE.test(p.code) && "border-destructive",
                    )}
                    aria-label="溯源码"
                  />
                  <button
                    type="button"
                    onClick={() => void p.embed()}
                    disabled={p.embedding || !p.embedFile || (!!p.code && !CODE_RE.test(p.code))}
                    className={cn(
                      "ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2",
                      "text-xs font-medium text-primary-foreground transition hover:opacity-90",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    {p.embedding ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Fingerprint className="h-3.5 w-3.5" />}
                    {p.embedding ? "植入中…" : "植入水印"}
                  </button>
                </div>
                {p.embedResult && (
                  <div className="space-y-2 rounded-xl border border-primary/25 bg-primary/5 p-3.5">
                    <p className="text-xs text-card-foreground">
                      已植入。溯源码{" "}
                      <span className="font-mono text-sm font-semibold text-primary">{p.embedResult.code}</span>
                      <span className="ml-2 text-muted-foreground">
                        （时长 {p.embedResult.duration_s}s · {p.embedResult.sr}Hz —— 请记下这个码，检测时要对得上）
                      </span>
                    </p>
                    <StudioAudioPlayer src={mediaUrl(p.embedResult.url)} label="水印后试听" />
                    <a
                      href={mediaUrl(p.embedResult.url)}
                      download={p.embedResult.wav}
                      className="inline-block text-xs text-primary hover:opacity-80"
                    >
                      下载加水印后的音频
                    </a>
                  </div>
                )}
              </div>
            </Card>
          </Section>

          <Section
            title="检测水印"
            desc="给一段音频，判断它带没带你的码。转成 mp3、微信发一圈之后通常仍能检出（检出是概率性的）。"
          >
            <Card className="p-5 sm:p-6">
              <div className="flex flex-col gap-3">
                <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
                  <input
                    ref={detectInputRef}
                    type="file"
                    accept="audio/*,.wav,.mp3,.flac,.ogg,.m4a,.aac,.opus,.webm"
                    onChange={(e) => p.changeDetectFile(e.target.files?.[0] ?? null)}
                    className="hidden"
                  />
                  <button
                    type="button"
                    onClick={() => detectInputRef.current?.click()}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3.5 py-2",
                      "text-xs text-card-foreground transition hover:border-primary",
                    )}
                  >
                    <ScanSearch className="h-3.5 w-3.5 text-primary" />
                    {p.detectFile ? p.detectFile.name : "选择音频文件…"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void p.detect()}
                    disabled={p.detecting || !p.detectFile}
                    className={cn(
                      "ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2",
                      "text-xs font-medium text-primary-foreground transition hover:opacity-90",
                      "disabled:cursor-not-allowed disabled:opacity-50",
                    )}
                  >
                    {p.detecting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ScanSearch className="h-3.5 w-3.5" />}
                    {p.detecting ? "检测中…" : "开始检测"}
                  </button>
                </div>
                {p.detectResult && (
                  <div
                    className={cn(
                      "flex items-start gap-2.5 rounded-xl border p-3.5 text-xs",
                      p.detectResult.watermarked
                        ? "border-primary/25 bg-primary/5"
                        : "border-border bg-muted/30",
                    )}
                  >
                    {p.detectResult.watermarked ? (
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                    ) : (
                      <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                    )}
                    <div className="space-y-1">
                      <p className="text-card-foreground">
                        {p.detectResult.watermarked ? "检出水印" : "未检出水印"}
                        （置信度 {p.detectResult.prob.toFixed(3)} ≥ 阈值 {p.detectResult.threshold}）
                      </p>
                      {p.detectResult.watermarked && (
                        <p>
                          溯源码{" "}
                          <span className="font-mono text-sm font-semibold text-primary">{p.detectResult.code}</span>
                        </p>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </Card>
          </Section>

          <Section
            title="合成自动加标识"
            desc="开着时，所有合成的语音（输字变声、微信发送、录课等）都会自动嵌上下面的码。默认关闭。"
          >
            <Card className="p-5 sm:p-6">
              <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
                <AutoSwitch
                  enabled={p.autoEnabled}
                  saving={p.autoSaving}
                  onToggle={(next) => void p.saveAuto(next, p.autoCode)}
                />
                <span className="font-mono text-sm text-card-foreground" aria-label="当前自动溯源码">
                  {p.autoEnabled ? `码：${p.autoCode || "（未生成）"}` : "未启用"}
                </span>
              </div>
              <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
                加标识失败时不会阻断合成 —— 语音照常生成，只是可能没带码（旁路语义，宁可缺码不可断链）。
              </p>
            </Card>
          </Section>
        </div>
      </PageShell>
    </div>
  )
}

/** 下载进度行：两文件各一行（mono 小字，不抢视线）。 */
function ProgressLine({ label, t }: { label: string; t: { status?: string; done?: number; total?: number | null } }) {
  const pct = t.total ? Math.min(100, Math.floor(((t.done ?? 0) / t.total) * 100)) : null
  return (
    <div className="flex items-center gap-2">
      <span className="w-20 shrink-0">{label}</span>
      <span className="w-20 shrink-0">{dlLabel(t)}</span>
      <span className="min-w-0 flex-1 truncate">{pct !== null ? `${pct}%` : ""}</span>
    </div>
  )
}

/** 开关（页面里唯一一枚；样式与全仓 toggle 一致）。 */
function AutoSwitch({
  enabled,
  saving,
  onToggle,
}: {
  enabled: boolean
  saving: boolean
  onToggle: (next: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      disabled={saving}
      onClick={() => onToggle(!enabled)}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full transition-colors",
        enabled ? "bg-primary" : "bg-muted-foreground/30",
        "disabled:cursor-not-allowed disabled:opacity-50",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 h-5 w-5 rounded-full bg-background shadow transition-transform",
          enabled ? "translate-x-[22px]" : "translate-x-0.5",
        )}
      />
    </button>
  )
}
