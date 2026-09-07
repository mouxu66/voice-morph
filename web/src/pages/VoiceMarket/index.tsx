import { useVoiceMarket } from "@/pages/VoiceMarket/useVoiceMarket"
import { MarketInstallBar, MarketPage } from "@/pages/VoiceMarket/VoiceMarketPage"

/** 音色市场：单页浏览，搜索框常驻顶部，精选与搜索结果共用同一列表。 */
export function VoiceMarketRoute() {
  const p = useVoiceMarket()

  return (
    <>
      <MarketPage {...p} />
      <MarketInstallBar {...p} />
    </>
  )
}
