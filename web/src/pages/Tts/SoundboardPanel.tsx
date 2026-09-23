import { useState } from "react"
import { Check, Loader2, Square, Wand2, X } from "lucide-react"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import {
  PREMIX_MODE_HINT,
  PREMIX_MODE_LABEL,
  type useSoundboard,
} from "@/pages/Tts/useSoundboard"

/** 出厂音效的图标（按 id；用户导入的走兜底）。 */
const ICONS: Record<string, string> = {
  boom: "💥",
  applause: "👏",
  alarm: "⚠️",
  riser: "🎵",
  ding: "🔔",
  weird: "👻",
}

/**
 * 特效声板格子面板（两种模式，同一份素材）。
 *
 * **实时**：点一下即出声，与人声一起被微信录走（详情见 `useSoundboard` 注释）。
 * **预混**：点一下只把音效勾上，点「混进这条语音」才离线混出一份新音频 ——
 *   发送目标随之换成它。这是"点格子会抢焦点、可能打断按住录音"那条路的兜底。
 *
 * **自门控**：整块 UI 归 `sound.fx-board` 插件，关掉它时后端 router 已卸载、
 * 端点全 404，所以这里必须 `return null` —— 这正是"宿主页托管别的插件"那一类
 * 缺口（`docs/犯错指南.md` 速查表 74 / `docs/犯错档案-工程.md` §8.40）。
 * 门控写在组件自己身上（而不是调用方），是为了让它无论被谁塞进哪个页面都安全。
 *
 * 两处使用点（都在微信发送页，宿主是 `hook.wechat`）：
 *   · ② 半自动 —— `allowPremix`：TTS 播到声卡、你按住 Alt 录制的**那几秒里**点格子，
 *     或者干脆先混好再播（确定性路径）；
 *   · ③ 手动  —— 只有实时：这一档没有合成产物，没有"要混的那条音频"。
 */
export function SoundboardPanel({
  sb,
  hint,
  allowPremix = false,
  wav,
  onPremixed,
}: {
  // hook 由路由层调一次（`pages/Tts/index.tsx`）往下传 —— 本组件在本页出现两回，
  // 各调一次 hook 就是两回预热/两回目录请求，而且两块的"正在播"高亮会各说各话。
  sb: ReturnType<typeof useSoundboard>
  hint?: string
  /** 是否提供"预混"模式（③ 手动档没有合成产物，只能实时）。 */
  allowPremix?: boolean
  /** 预混的源：要混的那条合成产物（文件名）。 */
  wav?: string
  /** 混好一份新音频时回调（调用方把发送目标换成它）。 */
  onPremixed?: (r: { wav: string; inserts: number; seconds: number }) => void
}) {
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const on = pluginVisible(catalog, "sound.fx-board")
  const [mode, setMode] = useState<"live" | "premix">("live")
  const [done, setDone] = useState("")

  if (!on) return null

  const premixUi = allowPremix && Boolean(wav)
  const inPremix = premixUi && mode === "premix"
  const picked = new Set(sb.picks.map((p) => p.sample))

  const runPremix = async () => {
    const r = await sb.premix(wav)
    if (r) {
      setDone(r.wav)
      onPremixed?.({ wav: r.wav, inserts: r.inserts, seconds: r.seconds })
      // 勾选**不清空**：常要换个位置再混一次（改勾选后重按按钮即可）。
    }
  }

  return (
    <div className="mt-3 rounded-md border border-border bg-background/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-card-foreground">
          音效声板
          <span className="ml-2 font-normal text-muted-foreground">
            {inPremix
              ? "勾选音效后混进这条语音（不抢焦点，确定性最高）"
              : (hint ?? "点一下即出声，与人声一起被微信录走")}
          </span>
        </p>
        <div className="flex items-center gap-2">
          {premixUi && (
            <div className="inline-flex overflow-hidden rounded-md border border-border text-[11px]">
              {(["live", "premix"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  className={`px-2 py-1 transition ${
                    mode === m
                      ? "bg-primary/15 text-primary"
                      : "text-muted-foreground hover:text-primary"
                  }`}
                >
                  {m === "live" ? "实时" : "预混"}
                </button>
              ))}
            </div>
          )}
          {sb.playing && !inPremix && (
            <button
              type="button"
              onClick={() => void sb.stop()}
              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              <Square className="h-3 w-3" />停止
            </button>
          )}
        </div>
      </div>

      <div className="mt-2 grid grid-cols-3 gap-2 sm:grid-cols-6">
        {sb.items.length === 0 && (
          <p className="col-span-3 text-[11px] text-muted-foreground sm:col-span-6">
            还没有音效素材（`sound.fx-board` 插件自带的 6 条出厂音效应在此列出）。
          </p>
        )}
        {sb.items.map((it) => {
          const active = inPremix ? picked.has(it.id) : sb.playing === it.id
          return (
            <button
              key={it.id}
              type="button"
              disabled={!sb.ready || sb.premixing}
              title={`${it.name} · ${it.duration_s}s${it.count ? ` · 用过 ${it.count} 次` : ""}${
                inPremix ? "（点一下勾选/取消）" : ""
              }`}
              onClick={() => void (inPremix ? sb.togglePick(it.id) : sb.play(it.id))}
              className={`relative flex flex-col items-center gap-1 rounded-lg border px-2 py-2.5 text-[11px] transition disabled:pointer-events-none disabled:opacity-50 ${
                active
                  ? "border-primary bg-primary/15 text-primary"
                  : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary"
              }`}
            >
              {inPremix && active && (
                <span className="absolute right-1 top-1 text-primary">
                  <Check className="h-3 w-3" />
                </span>
              )}
              <span className="text-lg leading-none">{ICONS[it.id] ?? "🎧"}</span>
              <span className="max-w-full truncate">{it.name}</span>
            </button>
          )
        })}
      </div>

      {/* 预混：勾选清单 + 位置轮换 + 执行 */}
      {inPremix && (
        <div className="mt-2.5 space-y-2">
          {sb.picks.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              {sb.picks.map((p) => (
                <span
                  key={p.sample}
                  className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/10 py-0.5 pl-2 pr-1 text-[11px] text-primary"
                >
                  <button
                    type="button"
                    onClick={() => sb.cyclePickMode(p.sample)}
                    title={`${PREMIX_MODE_HINT[p.mode]}（点一下切换位置）`}
                    className="inline-flex items-center gap-1"
                  >
                    {ICONS[p.sample] ?? "🎧"}
                    {PREMIX_MODE_LABEL[p.mode]}
                  </button>
                  <button
                    type="button"
                    onClick={() => sb.togglePick(p.sample)}
                    aria-label="取消这条音效"
                    className="opacity-70 transition hover:opacity-100"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          ) : (
            <p className="text-[11px] text-muted-foreground">
              点上面的格子勾选音效；「叠加」= 与人声同时响，「开头」= 先响一声再说话。
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={!sb.ready || !sb.picks.length || sb.premixing}
              onClick={() => void runPremix()}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-[11px] font-medium text-primary-foreground transition hover:scale-[1.03] disabled:pointer-events-none disabled:opacity-50"
            >
              {sb.premixing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
              {sb.premixing ? "混音中…" : "混进这条语音"}
            </button>
            {done && (
              <span className="font-mono text-[11px] text-muted-foreground">
                已混好 {done} · 改过勾选后再点一次即可
              </span>
            )}
          </div>
          {sb.premixError && <p className="text-[11px] text-destructive">{sb.premixError}</p>}
        </div>
      )}

      {sb.errorMessage && <p className="mt-2 text-[11px] text-destructive">{sb.errorMessage}</p>}
    </div>
  )
}
