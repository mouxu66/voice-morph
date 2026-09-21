import { useVoices } from "@/pages/Voices/useVoices"
import { VoicesPage } from "@/pages/Voices/VoicesPage"
import { useVoiceMarket } from "@/pages/VoiceMarket/useVoiceMarket"
import { MarketInstallBar, MarketPage } from "@/pages/VoiceMarket/VoiceMarketPage"
import { MergedPageTabs } from "@/components/MergedPageTabs"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"

/**
 * 音色库（合并页）：浏览/试听/试变自有音色（我的音色），
 * 以及下载社区开源音色（音色市场）。市场安装托盘提升到外层常驻，
 * 下载进行中切到「我的音色」tab 进度条不消失。
 *
 * 能力门控（C 类，2026-09-21 补）：本页路由属 `core.voices`（恒注册），但页内
 * 「音色挖掘」与「音色工坊」两块是**可关**能力（`sound.mine` / `sound.workshop`）。
 * 关掉后端点不再挂载，面板留着就是一路 404 —— 所以在这里读一次清单，把开关喂给
 * `useVoices(mineOn, workshopOn)`。清单没拿到（旧后端/加载中）按可见处理。
 */
export function VoicesRoute() {
  const { state: catalogState } = usePluginCatalog()
  const catalog = catalogState.status === "ready" ? catalogState.catalog : null
  const mineOn = pluginVisible(catalog, "sound.mine")
  const workshopOn = pluginVisible(catalog, "sound.workshop")
  const mk = useVoiceMarket()

  return (
    <>
      <MergedPageTabs
        tabs={[
          { key: "mine", label: "我的音色", content: <VoicesPage {...useVoices(mineOn, workshopOn)} /> },
          { key: "market", label: "音色市场", content: <MarketPage {...mk} /> },
        ]}
      />
      <MarketInstallBar {...mk} />
    </>
  )
}