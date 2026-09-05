import { MergedPageTabs } from "@/components/MergedPageTabs"
import { useVoiceMarket } from "@/pages/VoiceMarket/useVoiceMarket"
import { FeaturedTab, MarketInstallBar, SearchTab } from "@/pages/VoiceMarket/VoiceMarketPage"

/** 音色市场（合并页）：推荐清单 / 双源搜索，底部常驻安装进度条。 */
export function VoiceMarketRoute() {
  const p = useVoiceMarket()

  return (
    <>
      <MergedPageTabs
        tabs={[
          { key: "featured", label: "推荐", content: <FeaturedTab {...p} /> },
          { key: "search", label: "搜索", content: <SearchTab {...p} /> },
        ]}
      />
      <MarketInstallBar {...p} />
    </>
  )
}