import {
  Gamepad2,
  MessageCircle,
  Radio,
  Users,
  Loader2,
  type LucideIcon,
} from "lucide-react"
import type { LiveScene } from "@/api/client"
import { cn } from "@/lib/utils"

/**
 * 场景包卡片（2026-09-29）。
 *
 * 产品意图：用户心里想的是「我现在要开会」，不是「我要 index_rate=0.5、
 * 关掉字幕、开降噪」。这一层就是把后者翻译成前者。
 *
 * 三条 UI 上的实话（后端已把数据分好，别混着展示）：
 *  1. `in_settings` 是**持久**的；`on_start`（自我监听/字幕）**不持久**，
 *     只在应用那一刻随启动生效。所以卡片副标题写的是「本次生效」，不是「已保存」。
 *  2. `active` 为 null 时**不高亮任何卡片**。这不是「没选」的美化问题 ——
 *     用户点完场景又手动改了降噪，继续高亮就是骗他说「你还在开黑模式」。
 *  3. 应用是异步且可能重启变声，必须给 pending 态，否则用户会重复点。
 */

const ICONS: Record<string, LucideIcon> = {
  Gamepad2,
  MessageCircle,
  Radio,
  Users,
}

type Props = {
  scenes: LiveScene[]
  /** 当前高亮的场景键；null = 用户已手动偏离任何场景（不高亮） */
  active: string | null
  /** 正在应用的场景键（用于禁用与转圈） */
  pending?: string | null
  disabled?: boolean
  onApply: (key: string) => void
  className?: string
}

export function LiveScenePacks({
  scenes,
  active,
  pending = null,
  disabled = false,
  onApply,
  className,
}: Props) {
  if (!scenes.length) return null
  return (
    <div className={cn("grid gap-3 sm:grid-cols-2 xl:grid-cols-4", className)}>
      {scenes.map((s) => {
        const Icon = ICONS[s.icon ?? ""] ?? SparkleFallback
        const isActive = active === s.key
        const isPending = pending === s.key
        // 有别的场景正在应用时，禁止再点（后端会重启变声，并发点会打架）
        const blocked = disabled || (pending !== null && !isPending)
        return (
          <button
            key={s.key}
            type="button"
            disabled={blocked || isPending}
            onClick={() => onApply(s.key)}
            aria-pressed={isActive}
            data-scene={s.key}
            data-testid={`scene-${s.key}`}
            className={cn(
              "flex flex-col items-start gap-2 rounded-xl border p-4 text-left transition",
              "disabled:pointer-events-none disabled:opacity-60",
              isActive
                ? "border-primary/60 bg-primary/5 shadow-md"
                : "border-border bg-background/60 hover:border-primary/40 hover:bg-muted/50",
            )}
          >
            <span className="flex w-full items-center gap-2">
              {isPending ? (
                <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary" />
              ) : (
                <Icon
                  className={cn("h-4 w-4 shrink-0", isActive ? "text-primary" : "text-muted-foreground")}
                />
              )}
              <span
                className={cn(
                  "text-sm font-semibold",
                  isActive ? "text-primary" : "text-card-foreground",
                )}
              >
                {s.label}
              </span>
              {isActive && (
                <span className="ml-auto rounded-full bg-primary/15 px-2 py-0.5 text-[10px] font-medium text-primary">
                  当前
                </span>
              )}
            </span>
            <span className="text-xs leading-5 text-muted-foreground">{s.desc}</span>
          </button>
        )
      })}
    </div>
  )
}

/** 兜底图标：后端给了未知 icon 名时不至于整块崩掉（lucide 名是大驼峰，易拼错）。 */
function SparkleFallback(props: { className?: string }) {
  return <Radio {...props} />
}
