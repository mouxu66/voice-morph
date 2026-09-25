import { useCallback, useEffect, useState } from "react"
import { ArrowRight, Check, CircleAlert, Loader2, RefreshCw } from "lucide-react"
import { getAudioStatus, getHealth, sendChainCheck, ttsChainCheck } from "@/api/client"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { cn } from "@/lib/utils"

/**
 * 链路状态条 —— 把「先接通、再挑声音」这件事显式化。
 *
 * 为什么要它
 * ----------
 * 行业调研里最有价值的一条是 Voicemod 官方的上手次序（`docs/调研-用户需求与界面精简-2026-09-25.md`）：
 *   ① 装上虚拟麦克风 → ② 在你说话的 App 里把输入设备选成它 → ③ **然后才是选一个声音**
 *
 * 注意第 ③ 步排在最后。他们的链路只有"虚拟麦克风"一环，都还要先接通再挑声音；
 * 而我们的链路长得多（手机麦克风 → PC 推理 → 手机播放 / 或 PC 本地虚拟声卡 → 微信），
 * 却把"挑音色"放在了首屏第一件事 —— 用户挑完一个好听的声音，然后发现根本送不进
 * 目标应用，那种挫败比"界面丑"严重得多。
 *
 * 所以本组件在"挑声音"之前插一道：**告诉你这条链路现在通不通、断在哪一环**。
 *
 * 数据源都是现成的只读端点，没有新增后端接口：
 * - `/health`            → 后端服务本身活着没（`cuda` 为 null 表示没装 torch，不是故障）
 * - `/audio/status`      → 虚拟声卡（CABLE）在不在
 * - `/audio/send_chain`  → 发送链路逐项自检（items 与 /diagnose 同构）
 * - `/tts/send_chain`    → 输字变声链路逐项自检
 *
 * ⚠️ 三个别改坏的约定：
 * 1. **失败不炸页面**。任何一项探测抛异常都退化成一个"未知"格子，整页照常渲染 ——
 *    状态条本身就是为了诊断问题，它自己不能变成新的问题源。
 * 2. **不自动重试到死**。探测只在挂载时 + 用户手点刷新时发生。后端是主进程 spawn 的，
 *    窗口不等它，所以首次探测失败**是常态不是异常** —— 但轮询会白烧后端，交由用户决定。
 * 3. **`cuda: null` 不是故障**（types.ts 里明确写了）：没装 torch 时 `/health` 的
 *    `cuda` 就是 null，显示成灰格子而不是红格子。
 */
type Step = {
  key: string
  label: string
  /** "ok" 通 / "bad" 断 / "warn" 需要注意 / "unknown" 探不到 */
  state: "ok" | "bad" | "warn" | "unknown"
  detail: string
  hint?: string
  to?: string
}

export function ChainStatusBar({ className }: { className?: string }) {
  const [steps, setSteps] = useState<Step[] | null>(null)
  const [busy, setBusy] = useState(false)

  /**
   * 门控：本组件挂在**核心首页**上，却要探测两个**可关能力**的端点
   * （`/audio/send_chain` 属 `hook.wechat`、`/tts/send_chain` 属 `sound.tts`）。
   * 不门控的话，用户关掉其中一个就会在这里探到 404、
   * 被渲染成"链路断了" —— 明明是他自己关的，界面却在报故障。
   *
   * 这是本仓踩过的类型（核心页裸渲染可关组件），门禁
   * `audit_endpoint_ownership.py --check` 会直接报红。修法就是这里：
   * **面板自己判断**，被关掉的那格显示"未启用"而不是去发请求。
   */
  const { state: catalogState } = usePluginCatalog()
  const catalog = catalogState.status === "ready" ? catalogState.catalog : null
  const wechatOn = pluginVisible(catalog, "hook.wechat")
  const ttsOn = pluginVisible(catalog, "sound.tts")

  const probe = useCallback(async () => {
    setBusy(true)
    // 四项并行探测：任何一项失败都只影响它自己那一格（见约定 1）。
    // 被关掉的能力**不发请求**，直接给"未启用"那一格（见上文门控说明）。
    const [healthR, audioR, sendR, ttsR] = await Promise.allSettled([
      getHealth(),
      getAudioStatus(),
      wechatOn ? sendChainCheck() : Promise.reject(new Error("__off__")),
      ttsOn ? ttsChainCheck() : Promise.reject(new Error("__off__")),
    ])

    const next: Step[] = []

    // ① 后端本身
    if (healthR.status === "fulfilled") {
      const h = healthR.value
      next.push({
        key: "backend",
        label: "后端服务",
        state: h.status === "ok" ? "ok" : "warn",
        // cuda 为 null = 本环境没装 torch（可选依赖），不是"没有 GPU"
        detail: h.cuda === null ? "在跑（未装 torch，核心功能不受影响）" : h.cuda ? "在跑 · 可用 GPU" : "在跑 · 仅 CPU",
      })
    } else {
      next.push({
        key: "backend",
        label: "后端服务",
        state: "bad",
        detail: "没连上",
        hint: "后端可能还在启动（首次要几秒），点刷新再试；一直不行去「体检」。",
      })
    }

    // ② 虚拟声卡：从 capture 里找 CABLE（发送链路要靠它把声音灌进微信）
    if (audioR.status === "fulfilled") {
      const caps = Object.values(audioR.value.capture ?? {}).filter(Boolean) as string[]
      const hasCable = caps.some((d) => /cable/i.test(d))
      next.push({
        key: "cable",
        label: "虚拟声卡",
        state: hasCable ? "ok" : "warn",
        detail: hasCable ? "已就绪（CABLE）" : "没找到 CABLE",
        hint: hasCable ? undefined : "要发进微信/给别的软件用才需要它；只是打字试听可以先不管。",
      })
    } else {
      next.push({ key: "cable", label: "虚拟声卡", state: "unknown", detail: "探不到" })
    }

    // ③ 发送链路（微信那条）
    const pickChain = (
      r: PromiseSettledResult<{ items: { key: string; ok: boolean; label: string; detail: string; hint?: string | null }[] }>,
      label: string,
      enabled: boolean,
    ): Step => {
      // 能力被关掉 ≠ 链路断了。用 "unknown" 那一档显示（灰格子），文案说清是"没开"。
      if (!enabled) return { key: label, label, state: "unknown", detail: "该能力已关闭" }
      if (r.status !== "fulfilled") return { key: label, label, state: "unknown", detail: "探不到" }
      const bad = r.value.items.filter((i) => !i.ok)
      if (bad.length === 0) return { key: label, label, state: "ok", detail: `${r.value.items.length} 项检查全过` }
      return {
        key: label,
        label,
        state: "bad",
        detail: bad[0].label + "：" + bad[0].detail,
        hint: bad[0].hint ?? undefined,
      }
    }
    next.push(pickChain(sendR, "发送链路", wechatOn))
    next.push(pickChain(ttsR, "输字链路", ttsOn))

    setSteps(next)
    setBusy(false)
  }, [wechatOn, ttsOn])

  useEffect(() => {
    void probe()
  }, [probe])

  if (!steps) {
    return (
      <div className={cn("flex items-center gap-2 rounded-2xl border border-border bg-card/70 px-4 py-3", className)}>
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
        <span className="text-xs text-muted-foreground">正在检查链路…</span>
      </div>
    )
  }

  // 只有 "bad" 才算问题。"unknown"（含能力被关掉）不计入 —— 用户自己关的不该被报成故障。
  const badCount = steps.filter((s) => s.state === "bad").length

  return (
    <div className={cn("rounded-2xl border border-border bg-card/70 p-4", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-xs font-medium text-foreground">
          {badCount === 0 ? (
            <Check className="h-4 w-4 text-emerald-500" />
          ) : (
            <CircleAlert className="h-4 w-4 text-amber-500" />
          )}
          {badCount === 0 ? "链路是通的，可以直接挑声音了" : `有 ${badCount} 处还没接通`}
          <span className="font-normal text-muted-foreground">— 先接通，再用它说话</span>
        </p>
        <button
          type="button"
          onClick={() => void probe()}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-50"
        >
          <RefreshCw className={cn("h-3 w-3", busy && "animate-spin")} />
          重新检查
        </button>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {steps.map((s) => (
          <div
            key={s.key}
            className={cn(
              "rounded-xl border px-3 py-2.5",
              s.state === "ok" && "border-emerald-500/30 bg-emerald-500/5",
              s.state === "bad" && "border-destructive/40 bg-destructive/5",
              s.state === "warn" && "border-amber-500/35 bg-amber-500/5",
              s.state === "unknown" && "border-border bg-muted/30",
            )}
          >
            <p className="flex items-center gap-1.5 text-[11px] font-medium text-foreground">
              <span
                className={cn(
                  "inline-block h-1.5 w-1.5 shrink-0 rounded-full",
                  s.state === "ok" && "bg-emerald-500",
                  s.state === "bad" && "bg-destructive",
                  s.state === "warn" && "bg-amber-500",
                  s.state === "unknown" && "bg-muted-foreground/50",
                )}
              />
              {s.label}
            </p>
            <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{s.detail}</p>
            {s.hint && s.state !== "ok" && (
              <p className="mt-1 text-[10px] leading-4 text-muted-foreground/80">{s.hint}</p>
            )}
          </div>
        ))}
      </div>

      {badCount > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-3 border-t border-border pt-3">
          {/* ★ 这里此前是 `to="/home"` —— 而这个组件本身**就长在首页上**，
              点了等于原地跳一下，是条纯死链（用户 2026-09-25 点名）。
              改成本仓既有的跨组件事件约定（同 SettingsPanel 的 replay-* 系列）：
              派发事件 → App.tsx 收到后打开发送链路自检弹窗。
              为什么不直接 import 弹窗：弹窗的开关状态集中在 App.tsx（见那里的注释
              「弹窗开关集中在这里」），组件自己去开会让状态出现两个源。 */}
          <button
            type="button"
            onClick={() => window.dispatchEvent(new Event("open-send-chain"))}
            className="inline-flex items-center gap-1 text-[11px] font-medium text-primary transition hover:opacity-80"
          >
            去看逐项详情并修复 <ArrowRight className="h-3 w-3" />
          </button>
          <span className="text-[10px] text-muted-foreground">
            大部分"送不进去"的问题都能在那里一键修好
          </span>
        </div>
      )}
    </div>
  )
}
