import { AudioWaveform, CheckCircle2, Database, Radio, Sparkles } from "lucide-react"
import type { RvcVoice } from "@/api/client"
import { cn } from "@/lib/utils"

/**
 * 实时变声的音色选择器。
 *
 * 之前实时页用的是后端固定的"当前生效实验"，页面上根本没法选音色 —— 这就是
 * "在音色库选了美团骑士、实时页却不像"的根因。这里把音色选择显式化，
 * 并把每个音色走到哪一步（无语料 / 语料就绪 / 模型就绪 / 变声中）直接标在卡片上。
 */
type RvcVoicePickerProps = {
  voices: RvcVoice[]
  selectedId: string | null
  onSelect: (id: string) => void
  liveExp: string | null
  className?: string
}

function statusOf(v: RvcVoice): { label: string; tone: "ready" | "corpus" | "empty" } {
  if (v.model_ready) return { label: "模型就绪", tone: "ready" }
  if (v.dataset_count > 0) return { label: `语料 ${v.dataset_count} 条`, tone: "corpus" }
  return { label: v.has_reference ? "可生成语料" : "无参考音频", tone: "empty" }
}

export function RvcVoicePicker({ voices, selectedId, onSelect, liveExp, className }: RvcVoicePickerProps) {
  if (!voices.length) {
    return (
      <div className={cn("rounded-2xl border border-dashed border-border bg-card/60 p-6 text-center", className)}>
        <Sparkles className="mx-auto h-6 w-6 text-muted-foreground" />
        <p className="mt-3 text-sm font-medium text-card-foreground">还没有可用于实时变声的音色</p>
        <p className="mx-auto mt-1.5 max-w-md text-xs leading-5 text-muted-foreground">
          先到「音色库」挖掘或从勾选片段创建一个音色档案，然后回到这里为它生成语料、训练 RVC 模型。
        </p>
      </div>
    )
  }

  return (
    <div className={cn("-mx-1 flex snap-x gap-3 overflow-x-auto px-1 pb-2", className)}>
      {voices.map((v) => {
        const active = v.id === selectedId
        const live = v.id === liveExp
        const status = statusOf(v)
        return (
          <button
            key={v.id}
            type="button"
            onClick={() => onSelect(v.id)}
            aria-pressed={active}
            className={cn(
              "flex w-52 shrink-0 snap-start flex-col items-start gap-2 rounded-xl border p-3.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
              active
                ? "border-primary bg-primary/10 shadow-[0_0_0_1px_rgb(var(--c-accent)/0.35)]"
                : "border-border bg-card hover:border-primary/50",
            )}
          >
            <div className="flex w-full items-center justify-between gap-2">
              <span
                className={cn(
                  "flex h-9 w-9 items-center justify-center rounded-lg",
                  v.model_ready ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
                )}
              >
                <AudioWaveform className="h-4 w-4" />
              </span>
              {active && <CheckCircle2 className="h-4 w-4 text-primary" />}
            </div>

            <p className="w-full truncate text-sm font-semibold text-card-foreground" title={v.display_name}>
              {v.display_name}
            </p>
            <p className="w-full truncate font-mono text-[10px] text-muted-foreground" title={v.id}>
              {v.id}
            </p>

            <div className="flex flex-wrap items-center gap-1.5">
              <span
                className={cn(
                  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium",
                  status.tone === "ready" && "bg-emerald-500/15 text-emerald-600",
                  status.tone === "corpus" && "bg-primary/15 text-primary",
                  status.tone === "empty" && "bg-muted text-muted-foreground",
                )}
              >
                {status.tone === "ready" ? (
                  <CheckCircle2 className="h-3 w-3" />
                ) : status.tone === "corpus" ? (
                  <Database className="h-3 w-3" />
                ) : (
                  <Sparkles className="h-3 w-3" />
                )}
                {status.label}
              </span>
              {live && (
                <span className="inline-flex items-center gap-1 rounded-full bg-primary px-2 py-0.5 text-[10px] font-medium text-primary-foreground">
                  <Radio className="h-3 w-3 animate-pulse" />
                  变声中
                </span>
              )}
            </div>

            {v.model_ready && v.trained_at && (
              <p className="text-[10px] text-muted-foreground">训练于 {v.trained_at}</p>
            )}
          </button>
        )
      })}
    </div>
  )
}
