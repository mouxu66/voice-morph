import { useEffect, useRef, useState } from "react"
import { cn } from "@/lib/utils"

/**
 * 预混时间轴：把**源人声**画成一条波形，音效位置是波形上的可拖记号。
 *
 * 为什么要有它（2026-09-25 加）：在这之前"第几秒"只能靠一个数字输入框表达 ——
 * 而用户真正知道的是"说到那句的时候来一炮"，那是**看在眼里**的，不是数出来的。
 * 波形把"那句"变成可以看到的峰，拖一下就到位。
 *
 * 三条刻意的设计判断：
 *
 * 1. **纯前端解码，不加后端端点**：`fetch(wav) → decodeAudioData → 每桶取峰值`。
 *    后端只要多一个 `/peaks` 端点，就得在"素材目录枚举"之外再开一条读任意 outputs
 *    文件的口子；而浏览器本来就要把整段音频拿来才能画，没必要多一份服务端实现。
 *    顺带：`soundboard.py` 有没有这条端点与**放音**能力无关，缺了也不影响能用。
 * 2. **解码失败不清空能力**：取不到波形（环境没有 Web Audio、fetch 失败、文件已过期）
 *    时如实写出原因，**秒数输入框照旧可用** —— 拖不动不等于定不了位。
 * 3. **点轨道不移动任何记号**：多枚音效时"点一下该动谁"没有正确答案，
 *    误移比不移更糟（用户不会发现位置变了）。移动只有两个入口：拖记号本身、箭头键。
 *
 * 记号只画**「叠加」档**：开头/结尾是拼接，位置由档位本身决定（与
 * `useSoundboard` 的 `at_s` 只在 layer 分支生效是同一条口径）。
 */

/** 一个画在波形上的音效记号（只可能是叠加档）。 */
export type TimelineMarker = { id: string; label: string; at_s: number }

/** 采样 → `buckets` 根柱的归一化峰值（0~1）。 */
export function peaksFromChannel(channel: Float32Array, buckets: number): number[] {
  const n = Math.max(1, Math.floor(buckets))
  if (!channel || channel.length === 0) return new Array(n).fill(0)
  const step = channel.length / n
  const out: number[] = []
  for (let i = 0; i < n; i += 1) {
    const start = Math.floor(i * step)
    const end = Math.max(start + 1, Math.floor((i + 1) * step))
    let peak = 0
    for (let j = start; j < end && j < channel.length; j += 1) {
      const v = Math.abs(channel[j])
      if (v > peak) peak = v
    }
    out.push(peak)
  }
  // 归一化：画的是"这条音频的形状"，不是绝对响度 —— 一段整体偏轻的人声
  // 用绝对刻度会画成一条贴地直线，等于没有波形。
  const top = Math.max(...out, 1e-6)
  return out.map((v) => v / top)
}

/**
 * 秒数量化 + 夹紧。
 *
 * 量化不是省事：后端把秒数乘采样率取整，拖出 `3.0000000000000004` 这种值只会让日志难读；
 * 而 0.1s 已经远细于耳朵能分辨的位置差别。
 */
export function snapSeconds(value: number, duration: number, step = 0.1): number {
  const v = Number.isFinite(value) ? value : 0
  const hi = Number.isFinite(duration) && duration > 0 ? duration : Math.max(0, v)
  const snapped = Math.round(v / step) * step
  return Math.min(hi, Math.max(0, Number(snapped.toFixed(2))))
}

/** 指针的 x 落在第几秒（`left`/`width` 来自 `getBoundingClientRect()`）。 */
export function secondsAtX(
  clientX: number,
  left: number,
  width: number,
  duration: number,
  step = 0.1,
): number {
  if (!(width > 0) || !(duration > 0)) return 0
  const frac = (clientX - left) / width
  return snapSeconds(frac * duration, duration, step)
}

/** 秒刻度：最多 8 条，短音频细一点（1s/2s/5s/10s 四档）。 */
export function tickSeconds(duration: number): number[] {
  if (!(duration > 0)) return []
  const step = duration <= 8 ? 1 : duration <= 16 ? 2 : duration <= 40 ? 5 : 10
  const out: number[] = []
  for (let t = step; t < duration && out.length < 8; t += step) out.push(t)
  return out
}

/** 取一段音频的峰值与时长（解码失败返回 null，并把原因交给调用方）。 */
async function decodePeaks(src: string): Promise<{ peaks: number[]; duration: number }> {
  const Ctor =
    (window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext })
      .AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Ctor) throw new Error("这个环境没有 Web Audio")
  const res = await fetch(src)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const buf = await res.arrayBuffer()
  const ac = new Ctor()
  try {
    const audio = await ac.decodeAudioData(buf)
    return { peaks: peaksFromChannel(audio.getChannelData(0), 240), duration: audio.duration }
  } finally {
    try {
      void ac.close() // AudioContext 数量有上限：画完就关，别攒着
    } catch {
      /* 关不掉不影响已经画出来的东西 */
    }
  }
}

export function PremixTimeline({
  src,
  seconds,
  markers,
  onMove,
}: {
  /** 源人声的**可播放地址**（用 `mediaUrl()` 拼好的）；缺省时只能画刻度。 */
  src?: string
  /** 源时长（秒）：解码完成前先用它排布，避免波形一到就跳一下。 */
  seconds?: number
  markers: TimelineMarker[]
  onMove: (id: string, at_s: number) => void
}) {
  const [wave, setWave] = useState<{ peaks: number[]; duration: number } | null>(null)
  const [error, setError] = useState("")
  const boxRef = useRef<HTMLDivElement | null>(null)
  const dragging = useRef<string | null>(null)
  // 一枚记号都没有时不解码：整段 wav 拉下来只为画一条没人要看的波形，是白烧 CPU
  // （而且面板挂在页面上就一直挂着，用户可能压根没切到预混）。
  const active = markers.length > 0

  useEffect(() => {
    if (!active || !src) {
      setWave(null)
      setError("")
      return
    }
    let alive = true
    setWave(null)
    setError("") // 换源先清：否则新音频上会短暂显示旧波形，位置全错
    void decodePeaks(src)
      .then((r) => {
        if (alive) setWave(r)
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : "波形解码失败")
      })
    return () => {
      alive = false
    }
  }, [src, active])

  if (!active) return null

  const duration = wave?.duration ?? seconds ?? 0
  const moveTo = (clientX: number) => {
    const el = boxRef.current
    const id = dragging.current
    if (!el || !id || !(duration > 0)) return
    const rect = el.getBoundingClientRect()
    onMove(id, secondsAtX(clientX, rect.left, rect.width, duration))
  }

  return (
    <div className="space-y-1">
      <div
        ref={boxRef}
        data-testid="premix-timeline-track"
        className="relative h-16 overflow-hidden rounded-md border border-border bg-background"
      >
        {wave ? (
          <div className="absolute inset-0 flex items-center gap-px px-0.5">
            {wave.peaks.map((p, i) => (
              <span
                key={i}
                className="flex-1 rounded-sm bg-primary/35"
                style={{ height: `${Math.max(2, p * 100)}%` }}
              />
            ))}
          </div>
        ) : (
          <div className="absolute inset-0 flex items-center justify-center px-2 text-center text-[10px] text-muted-foreground">
            {error ? `波形取不到（${error}）—— 下面的秒数框仍可用来定位` : "波形加载中…"}
          </div>
        )}

        {duration > 0 &&
          tickSeconds(duration).map((t) => (
            <span
              key={t}
              className="absolute top-0 h-full border-l border-border/60"
              style={{ left: `${(t / duration) * 100}%` }}
            >
              <span className="absolute left-0.5 top-0.5 font-mono text-[9px] text-muted-foreground">
                {t}s
              </span>
            </span>
          ))}

        {markers.map((m) => (
          <button
            key={m.id}
            type="button"
            role="slider"
            aria-label={`${m.label} 插在第几秒`}
            aria-valuemin={0}
            aria-valuemax={Number(duration.toFixed(1))}
            aria-valuenow={m.at_s}
            title={`拖我改位置（也能在秒数框里填）· 当前第 ${m.at_s}s`}
            onPointerDown={(e) => {
              dragging.current = m.id
              try {
                e.currentTarget.setPointerCapture(e.pointerId)
              } catch {
                /* 没有指针捕获也能拖（只是快速甩出记号时会丢） */
              }
            }}
            onPointerMove={(e) => {
              if (dragging.current === m.id) moveTo(e.clientX)
            }}
            onPointerUp={(e) => {
              if (dragging.current === m.id) {
                moveTo(e.clientX)
                dragging.current = null
              }
            }}
            onPointerCancel={() => {
              dragging.current = null
            }}
            onKeyDown={(e) => {
              const d = e.key === "ArrowLeft" ? -0.1 : e.key === "ArrowRight" ? 0.1 : 0
              if (!d || !(duration > 0)) return
              e.preventDefault()
              onMove(m.id, snapSeconds(m.at_s + d, duration))
            }}
            className={cn(
              "absolute top-0 flex h-full w-4 -translate-x-1/2 cursor-ew-resize flex-col items-center",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
            )}
            style={{ left: `${duration > 0 ? (m.at_s / duration) * 100 : 0}%` }}
          >
            <span className="h-full w-0.5 bg-primary" />
            <span className="absolute top-0 whitespace-nowrap rounded-full border border-primary/50 bg-primary/15 px-1 text-[10px] leading-4 text-primary">
              {m.label} {m.at_s}s
            </span>
          </button>
        ))}
      </div>
      <p className="text-[10px] text-muted-foreground">
        拖动波形上的记号（或选中后按 ← →）改音效位置；刻度以秒为单位。
      </p>
    </div>
  )
}
