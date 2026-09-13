import { useVoices } from "@/pages/Voices/useVoices"
import { VoicesPage } from "@/pages/Voices/VoicesPage"
import { useVoiceMarket } from "@/pages/VoiceMarket/useVoiceMarket"
import { MarketInstallBar, MarketPage } from "@/pages/VoiceMarket/VoiceMarketPage"
import { MergedPageTabs } from "@/components/MergedPageTabs"

/**
 * 音色库（合并页）：浏览/试听/试变自有音色（我的音色），
 * 以及下载社区开源音色（音色市场）。市场安装托盘提升到外层常驻，
 * 下载进行中切到「我的音色」tab 进度条不消失。
 */
export function VoicesRoute() {
  const mk = useVoiceMarket()

  return (
    <>
      <MergedPageTabs
        tabs={[
          { key: "mine", label: "我的音色", content: <VoicesPage {...useVoices()} /> },
          { key: "market", label: "音色市场", content: <MarketPage {...mk} /> },
        ]}
      />
      <MarketInstallBar {...mk} />
    </>
  )
}