import { useEffect, useRef } from "react"
import { useSearchParams } from "react-router-dom"
import { MergedPageTabs } from "@/components/MergedPageTabs"
import { useTts } from "@/pages/Tts/useTts"
import { TtsPage } from "@/pages/Tts/TtsPage"
import { useAudiobook } from "@/pages/Audiobook/useAudiobook"
import { AudiobookPage } from "@/pages/Audiobook/AudiobookPage"

/**
 * 语音合成（合并页）：单段合成 与 有声书（长文本按句合成+拼接）两个模式。
 * 兼容旧深链：/tts?text= 自动填入单段文本框；/audiobook 路由已重定向到 ?tab=book。
 */
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

  return (
    <MergedPageTabs
      tabs={[
        { key: "single", label: "单段合成", content: <TtsPage {...tts} /> },
        { key: "book", label: "有声书（长文本）", content: <AudiobookPage {...useAudiobook()} /> },
      ]}
    />
  )
}
