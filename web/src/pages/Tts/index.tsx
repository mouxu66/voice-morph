import { useEffect, useRef } from "react"
import { useSearchParams } from "react-router-dom"
import { MergedPageTabs } from "@/components/MergedPageTabs"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { useTts } from "@/pages/Tts/useTts"
import { TtsPage } from "@/pages/Tts/TtsPage"
import { useAudiobook } from "@/pages/Audiobook/useAudiobook"
import { AudiobookPage } from "@/pages/Audiobook/AudiobookPage"
import { useCourse } from "@/pages/Course/useCourse"
import { CoursePage } from "@/pages/Course/CoursePage"
import { useSoundboard } from "@/pages/Tts/useSoundboard"
import { useWechatSend } from "@/pages/Tts/useWechatSend"
import { WechatSendPage } from "@/pages/Tts/WechatSendPage"

/**
 * 语音合成（合并页）：单段合成、有声书（长文本按句合成+拼接）、
 * 讲稿录课（PPT 备注逐页旁白）、微信语音发送四个模式。
 * 兼容旧深链：/tts?text= 自动填入单段文本框；/audiobook → ?tab=book；/wechat → ?tab=wechat。
 *
 * 「有声书」「讲稿录课」与「微信发送」tab 各自对应可关能力
 * （sound.audiobook / sound.course / hook.wechat）：按能力清单隐藏 ——
 * 关掉后后端端点不再挂载，留着 tab 只会一路 404。
 * 两个录课类 tab 的 label 按**产出形态**区分（一条长音频 / 逐页独立音轨），
 * 重名过一次（都叫「录课」），`index.gate.test.ts` 有断言防它合回去。
 *
 * ⚠️ `sound.audiobook` **不在** `plugin_manifest.BODY_IDS`，所以标准预设下是关的。
 * 「声音保存」页有一条「去录一节课」深链直接落在这里（`?tab=book`），那边会先按
 * 能力清单判断、关着就给一键开启 —— 两处必须一起看，别单改一边。
 *
 * 特效声板（`sound.fx-board`）挂在微信发送页内部（②③ 两处），所以它既要宿主开着，
 * 也要自己开着：`useSoundboard` 的 enabled = 两者相与（hook 调用点只在路由层这一次）。
 */
export function TtsRoute() {
  const [params] = useSearchParams()
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const bookOn = pluginVisible(catalog, "sound.audiobook")
  const courseOn = pluginVisible(catalog, "sound.course")
  const wechatOn = pluginVisible(catalog, "hook.wechat")
  const fxBoardOn = pluginVisible(catalog, "sound.fx-board")

  // hook 必须无条件调用（React 规则）；enabled=false 时它们内部直接不轮询。
  const tts = useTts()
  const book = useAudiobook(bookOn)
  const course = useCourse(courseOn)
  const wechat = useWechatSend(wechatOn)
  // 声板（素材目录）在**单段合成/有声书/微信**三处都要用（`SfxMarkBar` 的素材名
  // 就从它来），所以只要还有任一宿主 tab 开着就启用它 —— 以前只认微信，
  // 结果是"只开有声书"时工具栏拿不到素材，整块不渲染。
  const soundboard = useSoundboard((bookOn || wechatOn) && fxBoardOn)
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
        { key: "single", label: "单段合成", content: <TtsPage {...tts} soundboard={soundboard} /> },
        ...(bookOn
          ? [
              {
                key: "book",
                // 「录课 / 有声书」曾与隔壁「讲稿录课（PPT）」重名，两个 tab 都叫
                // 录课、用户分不清哪个是长文拼接 —— 这里按**产出形态**命名：
                // 这个拼成一条完整音频，讲稿录课是逐页独立音轨。
                label: "有声书（长文本）",
                content: <AudiobookPage {...book} soundboard={soundboard} />,
              },
            ]
          : []),
        ...(courseOn
          ? [
              {
                key: "course",
                label: "讲稿录课（PPT）",
                content: <CoursePage {...course} />,
              },
            ]
          : []),
        ...(wechatOn
          ? [
              {
                key: "wechat",
                label: "微信发送",
                content: <WechatSendPage {...wechat} soundboard={soundboard} />,
              },
            ]
          : []),
      ]}
    />
  )
}
