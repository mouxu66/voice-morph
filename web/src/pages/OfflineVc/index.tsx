import { MergedPageTabs } from "@/components/MergedPageTabs"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
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
 *
 * 能力门控（2026-09-23 补）：「效果器」是**另一个可关能力** `sound.effects`
 * （`套用` / `目录` 两个端点都在 `effects.py`），而本页路由属 `sound.offline-vc`。
 * 关掉 `sound.effects` 时本页仍开着，留着 tab 就是一路 404。
 * 前两个 tab（离线变声 / 表达力变声）都属本页自己的 `sound.offline-vc`，无需门控。
 */
export function OfflineVcRoute() {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const fxOn = pluginVisible(catalog, "sound.effects")

  // hook 必须无条件调用（React 规则）；enabled=false 时它们内部直接不打后端。
  const ovc = useOfflineVc()
  const seed = useSeedVc()
  const fx = useEffects(fxOn)

  return (
    <MergedPageTabs
      tabs={[
        { key: "ovc", label: "离线变声", content: <OfflineVcPage {...ovc} /> },
        { key: "seedvc", label: "表达力变声", content: <SeedVcPage {...seed} /> },
        ...(fxOn ? [{ key: "fx", label: "效果器", content: <EffectsPage {...fx} /> }] : []),
      ]}
    />
  )
}
