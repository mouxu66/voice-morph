import { useVoices } from "@/pages/Voices/useVoices"
import { VoicesPage } from "@/pages/Voices/VoicesPage"

export function VoicesRoute() {
  return <VoicesPage {...useVoices()} />
}
