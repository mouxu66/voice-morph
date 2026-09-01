import { MergedPageTabs } from "@/components/MergedPageTabs"
import { useLive } from "@/pages/Live/useLive"
import { LivePage } from "@/pages/Live/LivePage"
import { useCascade } from "@/pages/Cascade/useCascade"
import { CascadePage } from "@/pages/Cascade/CascadePage"

/**
 * 实时变声（合并页）：RVC 模型实时 与 级联换嗓（ASR→TTS）两个模式。
 * ?text= 透传给 TTS 页的场景不受影响；tab 用 ?tab= 区分。
 */
export function LiveRoute() {
  return (
    <MergedPageTabs
      tabs={[
        { key: "rvc", label: "RVC 实时（低延迟）", content: <LivePage {...useLive()} /> },
        { key: "cascade", label: "级联换嗓（更像换人）", content: <CascadePage {...useCascade()} /> },
      ]}
    />
  )
}
