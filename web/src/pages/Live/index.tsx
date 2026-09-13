import { useLive } from "@/pages/Live/useLive"
import { LivePage } from "@/pages/Live/LivePage"
import { useCascade } from "@/pages/Cascade/useCascade"
import { CascadePage } from "@/pages/Cascade/CascadePage"
import { MergedPageTabs } from "@/components/MergedPageTabs"

/**
 * 实时变声（合并页）：RVC 实时逐帧套音色（保留说话习惯，适合真人音色/低延迟），
 * 千问变声走 ASR→TTS 换嗓（适合卡通/角色音色），两种都实时。
 */
export function LiveRoute() {
  return (
    <MergedPageTabs
      tabs={[
        { key: "rvc", label: "RVC 实时", content: <LivePage {...useLive()} /> },
        { key: "qwen", label: "千问变声", content: <CascadePage {...useCascade()} /> },
      ]}
    />
  )
}