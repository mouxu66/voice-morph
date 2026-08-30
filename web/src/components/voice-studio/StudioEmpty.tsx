import { RefreshCw } from "lucide-react"

type StudioEmptyProps = { title: string; description: string; onRetry?: () => void }

/** 品牌声波插画：内联 SVG 自绘（无外部素材，零版权负担）。柱高取自
 *  一段人声能量包络，中心镜像对称；描边用主题品牌色，随暗/亮主题翻转。 */
function VoiceWaveArt() {
  const bars = [5, 9, 14, 8, 18, 12, 24, 15, 28, 19, 22, 11, 16, 7, 12, 6]
  return (
    <svg
      viewBox="0 0 240 64"
      className="mx-auto h-16 w-60"
      role="img"
      aria-label="声波装饰"
    >
      <defs>
        <linearGradient id="ve-wave" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="rgb(var(--c-accent) / 0.25)" />
          <stop offset="50%" stopColor="rgb(var(--c-accent) / 0.85)" />
          <stop offset="100%" stopColor="rgb(var(--c-accent-2) / 0.45)" />
        </linearGradient>
      </defs>
      {bars.map((h, i) => {
        const x = 12 + i * 14
        const grow = 1 - Math.abs(i - bars.length / 2) / (bars.length / 2) // 中心最高
        const hh = Math.max(4, h * (0.45 + 0.55 * grow))
        return (
          <rect
            key={i}
            x={x}
            y={32 - hh / 2}
            width={6}
            height={hh}
            rx={3}
            fill="url(#ve-wave)"
            className="transition-[opacity,transform] duration-500"
            style={{ opacity: 0.35 + 0.65 * grow, transformOrigin: `${x + 3}px 32px` }}
          />
        )
      })}
    </svg>
  )
}

export function StudioEmpty({ title, description, onRetry }: StudioEmptyProps) {
  return (
    <div className="rounded-2xl border border-dashed border-border bg-card/60 px-6 py-14 text-center shadow-md">
      <VoiceWaveArt />
      <p className="mt-5 text-sm font-medium text-card-foreground">{title}</p>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{description}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="mt-5 inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary">
          <RefreshCw className="h-3.5 w-3.5" />重试加载
        </button>
      )}
    </div>
  )
}
