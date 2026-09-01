import { MergedPageTabs } from "@/components/MergedPageTabs"
import { useOfflineVc } from "@/pages/OfflineVc/useOfflineVc"
import { OfflineVcPage } from "@/pages/OfflineVc/OfflineVcPage"
import { useEffects } from "@/pages/Effects/useEffects"
import { EffectsPage } from "@/pages/Effects/EffectsPage"

/**
 * 离线工坊（合并页）：对已有音频做后处理的两道工序。
 * 离线变声（RVC 整段换声）→ 效果器（混响/电话音/机器人等链式加工）。
 */
export function OfflineVcRoute() {
  return (
    <MergedPageTabs
      tabs={[
        { key: "ovc", label: "离线变声", content: <OfflineVcPage {...useOfflineVc()} /> },
        { key: "fx", label: "效果器", content: <EffectsPage {...useEffects()} /> },
      ]}
    />
  )
}
