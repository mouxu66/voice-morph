/** 音色标签所需的最小字段（兼容 /voices 的 VoiceInfo 与 /rvc/voices 的 RvcVoice） */
export type VoiceLike = {
  id: string
  display_name?: string | null
  source?: string
  source_license?: string
  license_source?: string
}

/**
 * 音色来源标签：市场安装 → 市场；其余（自己训练 / 本地导入）→ 自训。
 * 判定依据为后端 source 字段（市场安装落 logs/<id>/source.json）。
 */
export function voiceSourceTag(v: { source?: string }): "市场" | "自训" {
  return v.source === "market" ? "市场" : "自训"
}

/**
 * 市场音色的许可说明（G4）。返回 null 表示"没什么可说的"，调用方不渲染。
 *
 * 为什么三态要分开写而不是统一成"未标注"：它们的**后续动作不同** ——
 * `unlabeled` 是"查过了，上游没写"，法律上默认保留所有权利，到此为止；
 * `unreachable` 是"这次没查成"，换个网络还能再查。混成一句话会让人白费功夫，
 * 或者更糟：把"没查成"当成"已确认可用"。
 *
 * 文案与后端 `market_license.describe()` 保持同构（后端负责日志/API，这里负责 UI）。
 */
export function voiceLicenseNote(v: Pick<VoiceLike, "source_license" | "license_source">): string | null {
  const kind = v.license_source
  if (!kind) return null
  if (kind === "model-card" && v.source_license) {
    return `上游许可：${v.source_license}（读自模型卡）`
  }
  if (kind === "unlabeled") {
    return "上游未标注许可：仅供个人学习研究，勿商用（未标注即默认保留所有权利）"
  }
  return "上游许可未能读取：仅供个人学习研究，勿商用"
}

/**
 * 下拉框选项文案：中文名 + 来源标注，如「卡通·懒羊羊（市场）」「袋鼠骑士 v2（自训）」。
 * 原生 <option> 无法渲染彩色徽标，故用括号后缀区分来源。
 */
export function voiceOptionLabel(v: VoiceLike): string {
  const name = v.display_name?.trim() || v.id
  return `${name}（${voiceSourceTag(v)}）`
}
