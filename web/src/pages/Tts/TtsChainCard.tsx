import { AudioLines, Loader2, RefreshCw } from "lucide-react"
import { ChainResultList } from "@/components/ChainResultList"
import type { TtsChainInfo } from "@/types"

/**
 * 输字变声链路自检卡 —— 与实时变声页的 SEND CHAIN 同规格。
 *
 * 为什么需要：首页 STEP 0 首推的就是「输字让它说」，但这条链路此前没有任何自检。
 * 用户被推荐去的那条路，反而成了故障时最没指引的那条。
 *
 * 与实时变声的区别：那边查声卡（虚拟声卡/默认录音/播放设备），
 * 这边查引擎（模型/进程）与素材（参考音/输出目录）—— 故障面完全不同。
 */
export function TtsChainCard(p: {
  chain: TtsChainInfo | null
  loading: boolean
  onRecheck: () => void
}) {
  // 后端没起来时不占位（与实时变声页一致）
  if (!p.chain && !p.loading) return null

  return (
    <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">TTS CHAIN</p>
          <h3 className="mt-1 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <AudioLines className="h-4 w-4 text-primary" />
            输字变声链路自检
          </h3>
          <p className="mt-1.5 max-w-xl text-xs leading-5 text-muted-foreground">
            进页面自动查一遍：语音引擎、当前音色有没有参考音、结果能不能存下来。缺什么这里会直接说。
          </p>
        </div>
        <button
          type="button"
          onClick={p.onRecheck}
          disabled={p.loading}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
        >
          {p.loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          {p.loading ? "检查中…" : "重新检测"}
        </button>
      </div>

      {p.loading && !p.chain ? (
        <p className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
          正在检查语音引擎与音色…
        </p>
      ) : p.chain ? (
        <ChainResultList
          items={p.chain.items}
          allOk={p.chain.all_ok}
          allOkText="链路已就绪：语音引擎、当前音色的参考音、保存目录都正常，直接输字就能合成。"
        />
      ) : null}
    </section>
  )
}
