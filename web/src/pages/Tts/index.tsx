import { useEffect, useRef } from "react"
import { useSearchParams } from "react-router-dom"
import { useTts } from "@/pages/Tts/useTts"
import { TtsPage } from "@/pages/Tts/TtsPage"

export function TtsRoute() {
  const [params] = useSearchParams()
  const tts = useTts()
  const seeded = useRef(false)

  useEffect(() => {
    const text = params.get("text")
    if (text && !seeded.current) {
      tts.setText(text)
      seeded.current = true
    }
  })

  return <TtsPage {...tts} />
}