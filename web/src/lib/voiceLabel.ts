/** 音色标签所需的最小字段（兼容 /voices 的 VoiceInfo 与 /rvc/voices 的 RvcVoice） */
export type VoiceLike = {
  id: string
  display_name?: string | null
  source?: string
}

/**
 * 音色来源标签：市场安装 → 市场；其余（自己训练 / 本地导入）→ 自训。
 * 判定依据为后端 source 字段（市场安装落 logs/<id>/source.json）。
 */
export function voiceSourceTag(v: { source?: string }): "市场" | "自训" {
  return v.source === "market" ? "市场" : "自训"
}

/**
 * 下拉框选项文案：中文名 + 来源标注，如「卡通·懒羊羊（市场）」「袋鼠骑士 v2（自训）」。
 * 原生 <option> 无法渲染彩色徽标，故用括号后缀区分来源。
 */
export function voiceOptionLabel(v: VoiceLike): string {
  const name = v.display_name?.trim() || v.id
  return `${name}（${voiceSourceTag(v)}）`
}
