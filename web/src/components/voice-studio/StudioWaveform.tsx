import { cn } from "@/lib/utils"

type StudioWaveformProps = {
  tone?: "primary" | "muted" | "destructive"
  active?: boolean
  className?: string
}

export function StudioWaveform({ tone = "primary", active = false, className }: StudioWaveformProps) {
  const bars = [22, 42, 30, 58, 36, 72, 48, 84, 54, 66, 34, 76, 44, 60, 28, 52, 38, 70, 48, 62, 32, 78, 46, 56, 26, 44, 36, 68, 50, 74, 40, 58]
  return (
    <div className={cn("flex h-12 items-center gap-1", className)} aria-hidden="true">
      {bars.map((height, index) => (
        <span
          key={`${height}-${index}`}
          className={cn(
            "w-1 rounded-full transition-all duration-500",
            tone === "primary" && "bg-primary/70",
            tone === "muted" && "bg-muted-foreground/35",
            tone === "destructive" && "bg-destructive/60",
            active && index % 4 === 0 && "animate-pulse bg-primary",
          )}
          style={{ height: `${height}%` }}
        />
      ))}
    </div>
  )
}
