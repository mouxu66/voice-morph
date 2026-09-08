import { voiceSourceTag, type VoiceLike } from "@/lib/voiceLabel"

/**
 * 音色来源徽标：市场安装（绿）/ 自训或本地导入（紫）。
 * 用于卡片式选择器与音色库列表，让用户一眼分清音色是下载的还是自己训的。
 */
export function VoiceSourceBadge({ voice, className }: { voice: Pick<VoiceLike, "source">; className?: string }) {
  const tag = voiceSourceTag(voice)
  const tone =
    tag === "市场"
      ? "bg-emerald-500/15 text-emerald-400"
      : "bg-violet-500/15 text-violet-400"
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${tone} ${className ?? ""}`}
      title={tag === "市场" ? "从音色市场下载安装" : "自己训练或本地导入"}
    >
      {tag}
    </span>
  )
}
