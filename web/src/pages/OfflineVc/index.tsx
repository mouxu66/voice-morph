import { MergedPageTabs } from "@/components/MergedPageTabs"
import { useOfflineVc } from "@/pages/OfflineVc/useOfflineVc"
import { OfflineVcPage } from "@/pages/OfflineVc/OfflineVcPage"
import { useEffects } from "@/pages/Effects/useEffects"
import { EffectsPage } from "@/pages/Effects/EffectsPage"
import { useSeedVc } from "@/pages/SeedVc/useSeedVc"
import { SeedVcPage } from "@/pages/SeedVc/SeedVcPage"

/**
 * 离线工坊（合并页）：对已有音频做后处理的三道工序。
 * 离线变声（RVC 整段换声，可叠加 Seed-VC 情绪补偿）→ 表达力变声（Seed-VC 零样本，
 * 保留/转换语气情绪）→ 效果器（混响/电话音/机器人等链式加工）。
 */
export function OfflineVcRoute() {
  return (
    <MergedPageTabs
      tabs={[
        { key: "ovc", label: "离线变声", content: <OfflineVcPage {...useOfflineVc()} /> },
        { key: "seedvc", label: "表达力变声", content: <SeedVcPage {...useSeedVc()} /> },
        { key: "fx", label: "效果器", content: <EffectsPage {...useEffects()} /> },
      ]}
    />
  )
}
