import { usePetMarket } from "@/pages/PetMarket/usePetMarket"
import { PetInstallBar, PetMarketPage } from "@/pages/PetMarket/PetMarketPage"
import { PetMarketGuide } from "@/pages/PetMarket/PetMarketGuide"

/** 人偶市场：桌面人偶皮肤市场（开源素材，一键安装换肤）。 */
export function PetMarketRoute() {
  const p = usePetMarket()

  return (
    <>
      <PetMarketPage {...p} />
      <PetInstallBar {...p} />
      <PetMarketGuide />
    </>
  )
}