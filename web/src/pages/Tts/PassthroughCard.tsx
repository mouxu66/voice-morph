import { CircleAlert, Loader2, Mic, Power, Volume2 } from "lucide-react"

import { usePassthrough } from "@/pages/Tts/usePassthrough"

/**
 * 真声直讲开关：老师用**自己的真声**讲课的同时，热键插的 AI 音能叠加上去。
 *
 * 放在快捷短语板**上方**（同一页），因为两者是配套用的：
 * 短语板管"AI 替我说哪句"，这个开关管"真声也在里面"。
 *
 * ★ 最关键的是那段**外部软件设置提示**：声音进了虚拟声卡，但 QQ/微信默认听的是
 * 物理麦克风 —— 不改那一步，用户会以为功能坏了。所以它不能藏在 tooltip 里，
 * 必须在开关旁边直接写出来。
 */
export function PassthroughCard() {
  const pt = usePassthrough()

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-xs font-medium text-primary">边讲边插话</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <Mic className="h-4 w-4 text-primary" />
            真声直讲
          </h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            把麦克风的<strong className="text-foreground">真声原样</strong>送进虚拟声卡 ——
            上课时正常用自己声音讲，讲累了按 Ctrl+Alt+1~5，
            AI 音会<strong className="text-foreground">叠加上去</strong>，两边都听得到，不打断你说话。
          </p>
        </div>

        <button
          type="button"
          disabled={!pt.canUse || pt.busy}
          onClick={() => void pt.toggle()}
          aria-pressed={pt.running}
          className={`inline-flex shrink-0 items-center gap-2 rounded-md border px-4 py-2 text-sm font-medium transition disabled:pointer-events-none disabled:opacity-50 ${
            pt.running
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 hover:bg-emerald-500/20"
              : "border-primary/40 bg-primary/10 text-primary hover:bg-primary/20"
          }`}
        >
          {pt.busy ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Power className="h-4 w-4" />
          )}
          {pt.running ? "直讲中 · 点此停止" : "开启真声直讲"}
        </button>
      </div>

      {/* 运行态：把「声音送哪去」「从哪采」都摊开 —— 出问题时这是第一手线索 */}
      {pt.running && (
        <div className="mt-4 space-y-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-2.5">
          <p className="flex items-center gap-2 text-xs text-emerald-700">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
            正在把真声送进虚拟声卡
          </p>
          {pt.device && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Volume2 className="h-3.5 w-3.5 shrink-0" />
              送往：<span className="font-mono text-[11px] text-foreground">{pt.device}</span>
            </p>
          )}
          {pt.inputDevice && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Mic className="h-3.5 w-3.5 shrink-0" />
              采集：<span className="font-mono text-[11px] text-foreground">{pt.inputDevice}</span>
            </p>
          )}
        </div>
      )}

      {/* ★ 外部软件设置提示：不在本应用里做，但不做就没声音 —— 必须显式写出来 */}
      <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2.5">
        <p className="flex items-start gap-2 text-xs leading-5 text-amber-700">
          <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            还要在<strong>通话软件里</strong>把麦克风改成{" "}
            <span className="font-mono text-[11px]">CABLE Output (VB-Audio Virtual Cable)</span>
            <br />
            （QQ/微信 通话界面 → 麦克风下拉；不选这个，对面听到的还是你的物理麦克风，改了等于没改）
          </span>
        </p>
      </div>

      {pt.error && (
        <p className="mt-3 flex items-start gap-1.5 text-xs leading-5 text-destructive">
          <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {pt.error}
        </p>
      )}

      <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
        与「实时变声」「级联变声」互斥（都要占虚拟声卡），开着其中一个时无法开启。
      </p>
    </div>
  )
}
