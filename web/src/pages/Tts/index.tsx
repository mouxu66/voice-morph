import { useEffect, useRef } from "react"
import { useSearchParams } from "react-router-dom"
import { MergedPageTabs } from "@/components/MergedPageTabs"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { useTts } from "@/pages/Tts/useTts"
import { TtsPage } from "@/pages/Tts/TtsPage"
import { useAudiobook } from "@/pages/Audiobook/useAudiobook"
import { AudiobookPage } from "@/pages/Audiobook/AudiobookPage"
import { useWechatSend } from "@/pages/Tts/useWechatSend"
import { WechatSendPage } from "@/pages/Tts/WechatSendPage"

/**
 * 语音合成（合并页）：单段合成、有声书（长文本按句合成+拼接）、微信语音发送三个模式。
 * 兼容旧深链：/tts?text= 自动填入单段文本框；/audiobook → ?tab=book；/wechat → ?tab=wechat。
 *
 * 「有声书」与「微信发送」两个 tab 各自对应可关能力（sound.audiobook / hook.wechat）：
 * 按能力清单隐藏 —— 关掉后后端端点不再挂载，留着 tab 只会一路 404。
 */
export function TtsRoute() {
  const [params] = useSearchParams()
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const bookOn = pluginVisible(catalog, "sound.audiobook")
  const wechatOn = pluginVisible(catalog, "hook.wechat")

  // hook 必须无条件调用（React 规则）；enabled=false 时它们内部直接不轮询。
  const tts = useTts()
  const book = useAudiobook(bookOn)
  const wechat = useWechatSend(wechatOn)
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
        ...(bookOn
          ? [{ key: "book", label: "有声书（长文本）", content: <AudiobookPage {...book} /> }]
          : []),
        ...(wechatOn
          ? [{ key: "wechat", label: "微信发送", content: <WechatSendPage {...wechat} /> }]
          : []),
      ]}
    />
  )
}
