import { useLive } from "@/pages/Live/useLive"
import { LivePage } from "@/pages/Live/LivePage"

export function LiveRoute() {
  return <LivePage {...useLive()} />
}
