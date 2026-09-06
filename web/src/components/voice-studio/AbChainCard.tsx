import { useEffect, useRef, useState } from "react"
import { AudioLines, FileUp, Loader2, Network, ThumbsUp, Trophy, X } from "lucide-react"
import { abChainRun, mediaUrl } from "@/api/client"
import type { AbChainResult, ChainLink } from "@/api/client"
import type { VoiceInfo } from "@/types"
import { StudioAudioPlayer } from "./StudioAudioPlayer"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

const LINKS = [
  { key: "rvc", name: "RVC", desc: "保音高、整段转换，需要已训练模型" },
  { key: "seed_vc", name: "Seed-VC", desc: "零样本，无需训练，表达力换音色" },
  { key: "qwen3", name: "Qwen3-TTS", desc: "文本到语音克隆，需要填测试文本" },
] as const

/** 音色像度（CAM++ 声纹余弦）分档 */
function secsLabel(secs: number): { text: string; cls: string } {
  if (secs >= 0.85) return { text: "很像", cls: "text-emerald-600" }
  if (secs >= 0.75) return { text: "比较像", cls: "text-emerald-600" }
  if (secs >= 0.62) return { text: "一般", cls: "text-primary" }
  if (secs >= 0.5) return { text: "偏弱", cls: "text-amber-500" }
  return { text: "不像", cls: "text-destructive" }
}

function natsLabel(nats: number): { text: string; cls: string } {
  if (nats >= 3.4) return { text: "自然", cls: "text-emerald-600" }
  if (nats >= 3.0) return { text: "尚可", cls: "text-primary" }
  if (nats >= 2.6) return { text: "略生硬", cls: "text-amber-500" }
  return { text: "生硬/有损", cls: "text-destructive" }
}

const STATUS_TEXT: Record<string, string> = {
  done: "成功",
  failed: "失败",
  skipped: "跳过",
}

export function AbChainCard({ voices, backendUp }: { voices: VoiceInfo[]; backendUp: boolean }) {
  const [voiceId, setVoiceId] = useState("")
  const [file, setFile] = useState<File | null>(null)
  const [text, setText] = useState("")
  const [running, setRunning] = useState(false)
  const [error, setError] = useState("")
  const [result, setResult] = useState<AbChainResult | null>(null)
  const [pick, setPick] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!voices.length) return
    setVoiceId((cur) => (voices.some((v) => v.id === cur) ? cur : voices[0].id))
  }, [voices])

  const clearFile = () => {
    setFile(null)
    if (fileInputRef.current) fileInputRef.current.value = ""
  }

  const run = async () => {
    if (!voiceId || !file) return
    setRunning(true)
    setError("")
    setResult(null)
    setPick(null)
    try {
      setResult(await abChainRun(file, voiceId, text))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  // 客观分最高的一条链路（只比 done 且有 secs 的）
  const done = result
    ? LINKS.map((l) => ({ ...l, link: result.chains[l.key] })).filter(
        (x) => x.link.status === "done" && x.link.metrics,
      )
    : []
  const bestKey =
    done.length > 1
      ? done.reduce((a, b) => ((b.link.metrics?.secs ?? 0) > (a.link.metrics?.secs ?? 0) ? b : a)).key
      : null

  const renderLink = (name: string, desc: string, link: ChainLink, key: string) => {
    const m = link.metrics
    return (
      <div
        key={key}
        className={cn(
          "rounded-lg border bg-background/60 p-4 transition",
          key === bestKey ? "border-primary/70" : "border-border",
        )}
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="flex items-center gap-1.5 text-sm font-medium text-card-foreground">
              {name}
              {key === bestKey && <Trophy className="h-3.5 w-3.5 text-primary" />}
            </p>
            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{desc}</p>
          </div>
          <span
            className={cn(
              "shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium",
              link.status === "done"
                ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-500"
                : link.status === "skipped"
                  ? "border-border bg-muted text-muted-foreground"
                  : "border-red-500/40 bg-red-500/10 text-red-500",
            )}
          >
            {STATUS_TEXT[link.status] ?? link.status}
          </span>
        </div>

        {link.status === "done" && link.url && (
          <StudioAudioPlayer src={mediaUrl(link.url)} label="播放" className="mt-3" />
        )}

        {m && (
          <div className="mt-3 space-y-1 border-t border-border pt-2.5 text-[11px]">
            <p className="flex items-center justify-between">
              <span className="text-muted-foreground">音色像度 secs</span>
              <span className={cn("font-mono", secsLabel(m.secs).cls)}>
                {m.secs.toFixed(3)} · {secsLabel(m.secs).text}
              </span>
            </p>
            <p className="flex items-center justify-between">
              <span className="text-muted-foreground">自然度 nats</span>
              <span className={cn("font-mono", natsLabel(m.nats).cls)}>
                {m.nats.toFixed(3)} · {natsLabel(m.nats).text}
              </span>
            </p>
            <p className="flex items-center justify-between">
              <span className="text-muted-foreground">时长</span>
              <span className="font-mono text-muted-foreground">{m.duration_s}s</span>
            </p>
          </div>
        )}

        {link.error && (
          <p className="mt-2.5 text-[11px] leading-4 text-destructive">{link.error}</p>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-primary">CHAIN A/B</p>
          <h3 className="mt-2 text-lg font-semibold text-card-foreground">多链路对比评测</h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            同一段输入分别走 RVC / Seed-VC / Qwen3 三条链路，并排试听并给出客观分：
            <span className="text-card-foreground"> secs</span> 是输出与目标音色参考音的声纹相似度，
            <span className="text-card-foreground"> nats</span> 是自然度评分，两者都是越高越好。
          </p>
        </div>
        <Network className="h-5 w-5 text-primary" />
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-[1fr_2fr]">
        <div>
          <label className="text-xs font-medium text-muted-foreground" htmlFor="chain-voice">目标音色</label>
          <select
            id="chain-voice"
            value={voiceId}
            onChange={(e) => setVoiceId(e.target.value)}
            className="mt-1.5 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            {voices.map((v) => (
              <option key={v.id} value={v.id}>{v.display_name ?? v.id}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs font-medium text-muted-foreground" htmlFor="chain-text">测试文本（选填，填了才跑 Qwen3）</label>
          <input
            id="chain-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="例如：周末我们骑车去河边，看看落日再回来。"
            className="mt-1.5 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
          />
        </div>
      </div>

      <div className="mt-3">
        <label className="text-xs font-medium text-muted-foreground" htmlFor="chain-file">输入音频（源声，会被转成 16k 单声道）</label>
        <div className="mt-1.5 flex flex-wrap items-center gap-3">
          <label
            htmlFor="chain-file"
            className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-4 py-2.5 text-sm font-medium text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            <FileUp className="h-4 w-4" />选择音频
            <input
              ref={fileInputRef}
              id="chain-file"
              type="file"
              accept="audio/*,.wav,.mp3,.m4a,.webm,.flac"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
          {file ? (
            <span className="flex items-center gap-2 rounded-md border border-border bg-background/60 px-3 py-2 text-xs text-card-foreground">
              <AudioLines className="h-3.5 w-3.5 text-primary" />
              <span className="max-w-[16rem] truncate">{file.name}</span>
              <span className="text-muted-foreground">{(file.size / 1024 / 1024).toFixed(2)} MB</span>
              <button type="button" onClick={clearFile} className="text-muted-foreground transition hover:text-destructive" aria-label="移除音频">
                <X className="h-3.5 w-3.5" />
              </button>
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">未选择</span>
          )}
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void run()}
          disabled={running || !backendUp || !voiceId || !file}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
        >
          {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Network className="h-4 w-4" />}
          {running ? "跑链路中…（约 1–3 分钟）" : "开始多链路对比"}
        </button>
        {!file && <span className="text-xs text-muted-foreground">先选一段输入音频</span>}
      </div>

      {error && <ErrorPanel title="多链路对比失败" detail={error} className="mt-3" />}

      {result && (
        <div className="mt-5 space-y-3">
          <p className="text-xs text-muted-foreground">
            目标音色：<span className="text-card-foreground">{result.target_voice_id}</span>
            {result.text ? <> · 测试文本：{result.text}</> : " · 未填文本，Qwen3 不参与"}
          </p>
          <div className="grid gap-4 md:grid-cols-3">
            {LINKS.map((l) => renderLink(l.name, l.desc, result.chains[l.key], l.key))}
          </div>
          {done.length > 0 && (
            <div className="space-y-2 border-t border-border pt-4">
              <p className="text-xs text-muted-foreground">主观听感：听完觉得哪条最像？</p>
              <div className="flex flex-wrap items-center gap-2">
                {done.map((x) => (
                  <button
                    key={x.key}
                    type="button"
                    onClick={() => setPick(x.key)}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium transition",
                      pick === x.key
                        ? "border-primary bg-primary/15 text-primary"
                        : "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20",
                    )}
                  >
                    <ThumbsUp className="h-3.5 w-3.5" />
                    {x.name}
                  </button>
                ))}
                {pick && (
                  <button
                    type="button"
                    onClick={() => setPick(null)}
                    className="text-xs text-muted-foreground transition hover:text-primary"
                  >
                    重选
                  </button>
                )}
              </div>
              {pick && (
                <p className="text-xs leading-5">
                  {pick === bestKey ? (
                    <span className="text-emerald-600">
                      主观与客观一致：都选了 {LINKS.find((l) => l.key === pick)?.name}——这条链路可以定为默认。
                    </span>
                  ) : (
                    <span className="text-amber-500">
                      主观选 {LINKS.find((l) => l.key === pick)?.name}，客观 secs 最高是{" "}
                      {LINKS.find((l) => l.key === bestKey)?.name}——不一致时以听感为准，
                      secs 只衡量声纹接近度，抓不住咬字和节奏。
                    </span>
                  )}
                </p>
              )}
            </div>
          )}
          {bestKey && !pick && (
            <p className="text-xs leading-5 text-muted-foreground">
              客观分最高：<span className="text-primary">{LINKS.find((l) => l.key === bestKey)?.name}</span>
              （只比 secs；自然度与听感请按上面播放器自行判断）
            </p>
          )}
        </div>
      )}
    </div>
  )
}
