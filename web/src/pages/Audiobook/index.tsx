import { useAudiobook } from "@/pages/Audiobook/useAudiobook"
import { AudiobookPage } from "@/pages/Audiobook/AudiobookPage"

export function AudiobookRoute() {
  const ab = useAudiobook()
  return <AudiobookPage {...ab} />
}
