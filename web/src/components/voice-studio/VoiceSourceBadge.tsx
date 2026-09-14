import { voiceLicenseNote, voiceSourceTag, type VoiceLike } from "@/lib/voiceLabel"

/**
 * 音色来源徽标：市场安装（绿）/ 自训或本地导入（紫）。
 * 用于卡片式选择器与音色库列表，让用户一眼分清音色是下载的还是自己训的。
 *
 * 市场音色额外把**上游许可**挂进 title（G4）。为什么用 title 而不是再加个徽标：
 * 许可三态里有两种都表现为"没有许可名"，做成徽标会出现"两种灰点"这种看了等于
 * 没看的 UI；挂 title 既不占视觉，又能在悬停时给出完整来路。
 */
export function VoiceSourceBadge({ voice, className }: {
  voice: Pick<VoiceLike, "source" | "source_license" | "license_source">
  className?: string
}) {
  const tag = voiceSourceTag(voice)
  const tone =
    tag === "市场"
      ? "bg-emerald-500/15 text-emerald-400"
      : "bg-violet-500/15 text-violet-400"
  const license = tag === "市场" ? voiceLicenseNote(voice) : null
  const title = license
    ? `从音色市场下载安装 · ${license}`
    : tag === "市场"
      ? "从音色市场下载安装"
      : "自己训练或本地导入"
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[10px] font-medium ${tone} ${className ?? ""}`}
      title={title}
    >
      {tag}
    </span>
  )
}
