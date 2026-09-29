import { useState } from "react"
import { Activity, AudioLines, Waves } from "lucide-react"
import type { Lamp, LampItem, LampsResult } from "@/types"

// 音色体检「三灯」（2026-09-29）
// 数据来源：GET /api/voice/lamps（读落盘质检）/ POST /api/voice/lamps/scan（现场体检）。
// 合成逻辑在后端 tools/voice_report.py，阈值锚在实测素材分布上 —— 前端只负责显示，
// **不做任何分档判断**，否则前后端会出现两套阈值。

/** 三个灯各自的图标。key 与后端 voice_report.py 的 Lamp.key 对应。 */
const LAMP_ICON: Record<string, typeof Activity> = {
  similarity: AudioLines,
  naturalness: Activity,
  strain: Waves,
}

/**
 * 灯色 → 文本/边框类。
 * ★ 未测（lamp === null）单独一档灰色 —— 不能借红色或绿色，
 * 因为「没测到」与「不合格」是两件事，混在一起用户会去修不存在的问题。
 */
const LAMP_TONE: Record<string, { dot: string; text: string; ring: string; label: string }> = {
  green: {
    dot: "bg-emerald-400",
    text: "text-emerald-300",
    ring: "border-emerald-500/35 bg-emerald-500/10",
    label: "正常",
  },
  yellow: {
    dot: "bg-amber-400",
    text: "text-amber-300",
    ring: "border-amber-500/35 bg-amber-500/10",
    label: "偏弱",
  },
  red: {
    dot: "bg-red-400",
    text: "text-red-300",
    ring: "border-red-500/35 bg-red-500/10",
    label: "需复查",
  },
  untested: {
    dot: "bg-zinc-500",
    text: "text-zinc-400",
    ring: "border-zinc-600/40 bg-zinc-700/10",
    label: "未测",
  },
}

function toneOf(lamp: Lamp | null) {
  return LAMP_TONE[lamp ?? "untested"]
}

/** 单个灯：图标 + 名称 + 状态点。hover/点击展开该项的详细数值。 */
function LampCell({ item }: { item: LampItem }) {
  const tone = toneOf(item.lamp)
  const Icon = LAMP_ICON[item.key] ?? Activity
  const [open, setOpen] = useState(false)
  return (
    <button
      type="button"
      onClick={() => setOpen((v) => !v)}
      title={item.detail}
      className={`flex min-w-0 flex-1 items-center gap-1.5 rounded-md border px-2 py-1 text-left transition-colors ${tone.ring} hover:brightness-110`}
    >
      <Icon className={`h-3.5 w-3.5 shrink-0 ${tone.text}`} aria-hidden />
      <span className="min-w-0 flex-1 truncate text-[11px] text-zinc-300">{item.label}</span>
      <span className={`h-2 w-2 shrink-0 rounded-full ${tone.dot}`} aria-hidden />
      {open && (
        <span className="absolute z-20 mt-1 max-w-[260px] translate-y-6 rounded-md border border-zinc-700 bg-zinc-900/95 px-2 py-1.5 text-[11px] leading-relaxed text-zinc-300 shadow-lg">
          {item.detail}
        </span>
      )}
    </button>
  )
}

/**
 * 三灯一行 + 一句话结论。
 *
 * - `data` 为 null：还没跑过体检 → 显示「未体检」而不是三个灰灯，
 *   否则用户会以为「测了但都没测到」。
 * - `compact`：卡片列表里的紧凑形态（只留一行）。
 */
export function VoiceLamps({
  data,
  compact = false,
}: {
  data: LampsResult | null | undefined
  compact?: boolean
}) {
  if (!data || !data.lamps?.length) {
    return (
      <div className="flex items-center gap-1.5 text-[11px] text-zinc-500">
        <span className="h-2 w-2 rounded-full bg-zinc-600" aria-hidden />
        未体检
      </div>
    )
  }

  return (
    <div className={compact ? "space-y-1" : "space-y-1.5"}>
      <div className="flex items-stretch gap-1.5">
        {data.lamps.map((item) => (
          <LampCell key={item.key} item={item} />
        ))}
      </div>
      {!compact && (
        <p className="text-[11px] leading-relaxed text-zinc-400">
          {data.verdict}
          {data.tested_count < data.lamps.length && (
            <span className="text-zinc-500">
              {" "}
              · {data.tested_count}/{data.lamps.length} 项已测
            </span>
          )}
        </p>
      )}
    </div>
  )
}
