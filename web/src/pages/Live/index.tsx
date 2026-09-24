import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { useLive } from "@/pages/Live/useLive"
import { LivePage } from "@/pages/Live/LivePage"
import { useCascade } from "@/pages/Cascade/useCascade"
import { CascadePage } from "@/pages/Cascade/CascadePage"
import { MergedPageTabs } from "@/components/MergedPageTabs"
import { useSoundboard } from "@/pages/Tts/useSoundboard"

/**
 * 实时变声（合并页）：RVC 实时逐帧套音色（保留说话习惯，适合真人音色/低延迟），
 * 千问变声走 ASR→TTS 换嗓（适合卡通/角色音色），两种都实时。
 *
 * 特效声板（`sound.fx-board`）挂在**两个 tab 的控制台里** —— 这里是它的主场景：
 * 边变声边打音效。它写进 CABLE Input，与变声后的声音由系统混音器混合，微信/游戏从
 * CABLE Output 取到的就是混好的结果；开「自我监听」时自己耳机里也听得到
 * （回环进程 tap 的正是 CABLE Output）。实时链路是 WASAPI **共享**模式
 * （`D:/RVC/rvc_headless.py` 的 `sg_wasapi_exclusive: False`），两路能并发写同一设备。
 *
 * 门控 —— 这一页是「宿主页托管别的插件」的又一例，规矩与 Tts 页不同：
 *   · 本页属 `sound.rvc-live`，关掉它**整页都不存在**，所以**不按它门控**
 *     （同 OfflineVc 前两个 tab 的判据：过度门控 = 把还能用的功能从界面上藏掉）；
 *   · 声板是另一个可关插件，所以必须按 `sound.fx-board` 门控。
 *
 * hook 只在路由层调这一次：两个 tab 共用一份素材目录、一次预热。各调一次就是
 * 两回请求 + 两回预热，而且两块的「正在播」高亮会各说各话。
 */
export function LiveRoute() {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const fxBoardOn = pluginVisible(catalog, "sound.fx-board")

  // hook 必须无条件调用（React 规则）；enabled=false 时它内部既不预热也不拉目录。
  const live = useLive()
  const qwen = useCascade()
  const soundboard = useSoundboard(fxBoardOn)

  return (
    <MergedPageTabs
      tabs={[
        { key: "rvc", label: "RVC 实时", content: <LivePage {...live} soundboard={soundboard} /> },
        { key: "qwen", label: "千问变声", content: <CascadePage {...qwen} soundboard={soundboard} /> },
      ]}
    />
  )
}
