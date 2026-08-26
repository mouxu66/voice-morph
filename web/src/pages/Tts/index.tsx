import { useTts } from "@/pages/Tts/useTts"
import { TtsPage } from "@/pages/Tts/TtsPage"

export function TtsRoute() {
  return <TtsPage {...useTts()} />
}
