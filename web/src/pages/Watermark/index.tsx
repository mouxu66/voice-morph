import { WatermarkPage } from "./WatermarkPage"
import { useWatermark } from "./useWatermark"

export function WatermarkRoute() {
  return <WatermarkPage {...useWatermark()} />
}
