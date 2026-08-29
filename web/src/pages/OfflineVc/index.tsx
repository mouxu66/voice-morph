import { useOfflineVc } from "@/pages/OfflineVc/useOfflineVc"
import { OfflineVcPage } from "@/pages/OfflineVc/OfflineVcPage"

export function OfflineVcRoute() {
  const ovc = useOfflineVc()
  return <OfflineVcPage {...ovc} />
}
