import { useEffect, useState } from "react"
import { Eye, Loader2, Minus, RefreshCw, Scale, ThumbsUp } from "lucide-react"
import { abRun, mediaUrl } from "@/api/client"
import type { AbResult } from "@/api/client"
import type { VoiceInfo } from "@/types"
import { StudioAudioPlayer } from "./StudioAudioPlayer"
import { cn } from "@/lib/utils"

type Tag = "A" | "B"

/** 相似度（生成音频 vs 参考音频的声纹余弦）分档提示 */
function simLabel(sim: number): { text: string; cls: string } {
  if (sim >= 0.985) return { text: "几乎一致", cls: "text-emerald-600" }
  if (sim >= 0.97) return { text: "很像", cls: "text-emerald-600" }
  if (sim >= 0.94) return { text: "比较像", cls: "text-primary" }
  if (sim >= 0.9) return { text: "一般", cls: "text-amber-500" }
  return { text: "偏弱", cls: "text-destructive" }
}

export function AbCompareCard({ voices, backendUp }: { voices: VoiceInfo[]; backendUp: boolean }) {
  const [aId, setAId] = useState("")
  const [bId, setBId] = useState("")
  const [text, setText] = useState("")
  const [running, setRunning] = useState(false)
  const [error, setError] = useState("")
  const [result, setResult] = useState<AbResult | null>(null)
  // 打乱后的展示顺序：样本①/样本② 各对应哪个原始音色（盲听揭晓前不显示）
  const [order, setOrder] = useState<[Tag, Tag]>(["A", "B"])
  const [verdict, setVerdict] = useState<"1" | "2" | "tie" | null>(null)

  // 音色列表变化时补默认选项（前两个）
  useEffect(() => {
    if (!voices.length) return
    setAId((cur) => (voices.some((v) => v.id === cur) ? cur : voices[0].id))
    setBId((cur) => (voices.some((v) => v.id === cur) ? cur : (voices[1] ?? voices[0]).id))
  }, [voices])

  const nameOf = (id: string) => voices.find((v) => v.id === id)?.display_name ?? id

  const run = async () => {
    if (!aId || !bId || aId === bId) return
    setRunning(true)
    setError("")
    setResult(null)
    setVerdict(null)
    try {
      // 参考音频较长时单侧合成可能要几分钟（8GB 显卡 ICL 特性），浏览器 fetch 无超时，安心等
      const r = await abRun(aId, bId, text)
      // 每次运行都随机展示顺序，保证盲听有效
      setOrder(Math.random() < 0.5 ? ["A", "B"] : ["B", "A"])
      setResult(r)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(false)
    }
  }

  const show = (tag: Tag) => (order[0] === tag ? result?.A : result?.B)
  const revealed = verdict !== null

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-primary">A / B BLIND TEST</p>
          <h3 className="mt-2 text-lg font-semibold text-card-foreground">音色对比评分</h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            同一句话用两个音色各合成一遍，打乱后盲听打分；揭晓时附上声纹相似度（合成音频 vs 各自参考音频）。
          </p>
        </div>
        <Scale className="h-5 w-5 text-primary" />
      </div>

      <div className="mt-5 grid gap-3 sm:grid-cols-[1fr_1fr_2fr]">
        <div>
          <label className="text-xs font-medium text-muted-foreground" htmlFor="ab-a">音色 A</label>
          <select
            id="ab-a"
            value={aId}
            onChange={(e) => setAId(e.target.value)}
            className="mt-1.5 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            {voices.map((v) => (
              <option key={v.id} value={v.id} disabled={v.id === bId}>{v.display_name ?? v.id}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs font-medium text-muted-foreground" htmlFor="ab-b">音色 B</label>
          <select
            id="ab-b"
            value={bId}
            onChange={(e) => setBId(e.target.value)}
            className="mt-1.5 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            {voices.map((v) => (
              <option key={v.id} value={v.id} disabled={v.id === aId}>{v.display_name ?? v.id}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs font-medium text-muted-foreground" htmlFor="ab-text">测试文本（留空自动选一句）</label>
          <input
            id="ab-text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="例如：周末我们骑车去河边，看看落日再回来。"
            className="mt-1.5 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
          />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => void run()}
          disabled={running || !backendUp || !aId || !bId || aId === bId}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
        >
          {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Scale className="h-4 w-4" />}
          {running ? "合成中…（约 1–2 分钟）" : "开始盲听对比"}
        </button>
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>

      {result && (
        <div className="mt-5 space-y-4">
          <p className="text-xs text-muted-foreground">测试文本：{result.text}</p>
          <div className="grid gap-4 sm:grid-cols-2">
            {(["1", "2"] as const).map((slot) => {
              const tag = slot === "1" ? order[0] : order[1]
              const side = show(tag)
              const isWinner = verdict !== "tie" && verdict === slot
              return (
                <div
                  key={slot}
                  className={cn(
                    "rounded-lg border bg-background/60 p-4 transition",
                    revealed && isWinner ? "border-primary/70" : "border-border",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-sm font-medium text-card-foreground">样本{slot === "1" ? "①" : "②"}</p>
                    {revealed ? (
                      <div className="text-right">
                        <p className="text-xs font-medium text-primary">{nameOf(side?.voice_id ?? "")}</p>
                        <p className={cn("font-mono text-[11px]", simLabel(side?.similarity ?? 0).cls)}>
                          声纹相似度 {(side?.similarity ?? 0).toFixed(3)} · {simLabel(side?.similarity ?? 0).text}
                        </p>
                      </div>
                    ) : (
                      <Eye className="h-3.5 w-3.5 text-muted-foreground" />
                    )}
                  </div>
                  {side && <StudioAudioPlayer src={mediaUrl(side.url)} label="播放样本" className="mt-3" />}
                </div>
              )
            })}
          </div>

          {!revealed ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground">听完打分：</span>
              {([
                ["1", "样本①更像", ThumbsUp],
                ["tie", "差不多", Minus],
                ["2", "样本②更像", ThumbsUp],
              ] as const).map(([v, label, Icon]) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setVerdict(v)}
                  className="inline-flex items-center gap-1.5 rounded-md border border-primary/40 bg-primary/10 px-3 py-2 text-xs font-medium text-primary transition hover:bg-primary/20"
                >
                  <Icon className="h-3.5 w-3.5" />
                  {label}
                </button>
              ))}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
              <span>
                {verdict === "tie"
                  ? "你的判断：两个音色差不多"
                  : `你的判断：${verdict === "1" ? "样本①" : "样本②"}（${nameOf(show(verdict === "1" ? order[0] : order[1])?.voice_id ?? "")}）更像`}
              </span>
              <button
                type="button"
                onClick={() => setVerdict(null)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
              >
                <RefreshCw className="h-3 w-3" />
                重打分
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
