/**
 * 启动时的**全屏使用方式选择**（2026-10-03）。
 *
 * 与现有两个启动组件的分工 —— 三个都是「选完就走」，但维度不同：
 * - `UseCasePicker`（仅首次）：问「你**长期**用它干嘛」→ 影响首屏裁剪；
 *   首次启动时它先弹，本组件让位（见 shouldShowModeChooser）。
 * - `FirstLaunchGuide`（仅首次）：问「你想先干什么」→ 选完跳页。
 * - 本组件（**每次启动**）：问「**今天**要用哪种方式」→ 套用场景套餐 + 跳到入口页。
 *   选错随时能改：侧栏「使用模式」块就是同一件事的常驻版。
 *
 * 为什么首次启动不弹：首次已经有 UseCasePicker 占住全屏问答位，再叠一个
 * 等于连问三句 —— 第二次打开再问「今天干嘛」才自然。
 *
 * 「记住我的选择」落 localStorage（`vm.modechooser.dontask`），勾上后**本次的
 * 动作照做**（套套餐/跳页/直接进入），只是下次启动不再弹。
 */
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router"
import { ArrowRight, Check, Loader2 } from "lucide-react"
import { applyPluginPreset } from "@/api/client"
import { restartBackend } from "@/lib/electron"
import { usePluginCatalog } from "@/lib/pluginRoutes"
import { hasBeenAsked } from "@/lib/useProfile"
import { cn } from "@/lib/utils"
import { SCENE_META } from "@/components/layout/ModeSwitcher"

const DONT_ASK_KEY = "vm.modechooser.dontask"
/** 模式切换要**重启后端**才生效（路由/插件集在启动时挂载）——重载后由本组件消费、跳到目标页。 */
const PENDING_ENTRY_KEY = "vm.pendingEntry"
/** 重载后一次性不弹选择页（刚选完模式再弹一次等于骚扰）。sessionStorage：关掉应用就失效。 */
const SUPPRESS_KEY = "vm.modechooser.suppress-once"

export function shouldShowModeChooser(): boolean {
  try {
    return hasBeenAsked() && localStorage.getItem(DONT_ASK_KEY) !== "1"
  } catch {
    return false
  }
}

/** 每个场景选完落在哪：与后端各套餐的「入口页」一致（见 plugin_manifest.py 的语义）。 */
const MODE_ENTRY: Record<string, string> = {
  wechat: "/tts?tab=wechat",
  preserve: "/preserve",
  game: "/live",
}

export function ModeChooser() {
  const [open, setOpen] = useState(shouldShowModeChooser)
  const [dontAsk, setDontAsk] = useState(false)
  const [switching, setSwitching] = useState<string | null>(null)
  const { state: catalogState } = usePluginCatalog()
  const navigate = useNavigate()

  // 整页重载后本组件重新挂载，按序消费两件遗留：
  // ① 待进入页面（pick 换模式时存下）→ 跳过去，不再弹选择页
  // ② 一次性抑制标记（选完模式的重载）→ 只是不弹
  useEffect(() => {
    const pending = localStorage.getItem(PENDING_ENTRY_KEY)
    if (pending) {
      localStorage.removeItem(PENDING_ENTRY_KEY)
      navigate(pending)
      setOpen(false)
      return
    }
    if (sessionStorage.getItem(SUPPRESS_KEY) === "1") {
      sessionStorage.removeItem(SUPPRESS_KEY)
      setOpen(false)
    }
  }, [navigate])

  const finish = useCallback(() => {
    if (dontAsk) localStorage.setItem(DONT_ASK_KEY, "1")
    setOpen(false)
  }, [dontAsk])

  /**
   * 2026-10-03 修 bug：原先「发射后不管」地套套餐就跳页，但插件集与路由是
   * **后端启动时挂载的** —— 没重启，目标页路由根本不存在（被 `*` 兜底弹回首页），
   * catalog 缓存也没刷新（侧栏/顶栏还显示旧模式）。现在换成：
   * 套套餐 → 记下目标页 → 重启后端 → 整页重载（catalog 全新、路由齐全）→ 落到目标页。
   * 已是当前模式则不折腾，直接跳页。
   */
  const pick = useCallback(
    async (id: string) => {
      const cat = catalogState.status === "ready" ? catalogState.catalog : null
      const entry = MODE_ENTRY[id] ?? "/home"
      if (!cat || cat.preset === id) {
        if (dontAsk) localStorage.setItem(DONT_ASK_KEY, "1")
        setOpen(false)
        navigate(entry)
        return
      }
      setSwitching(id)
      try {
        await applyPluginPreset(id)
      } catch {
        /* 失败也继续重启：重载后以清单为准，套没套上界面上看得到（侧栏高亮不骗人） */
      }
      localStorage.setItem(PENDING_ENTRY_KEY, entry)
      sessionStorage.setItem(SUPPRESS_KEY, "1")
      try {
        await restartBackend()
      } catch {
        /* 纯网页调试没有重启通道 —— 重载后模式可能没换上，可从侧栏再切 */
      }
      location.reload()
    },
    [catalogState, dontAsk, navigate],
  )

  // Esc = 直接进入（与 UseCasePicker 同款键盘出口约定）；切换中不给 Esc 出口
  useEffect(() => {
    if (!open || switching) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finish()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, switching, finish])

  if (!open) return null

  const ready = catalogState.status === "ready"
  const scenes = ready ? catalogState.catalog.presets.filter((p) => p.id in SCENE_META) : []

  // 全屏**页面**而不是弹窗：不透明背景直接盖住侧栏/顶栏（用户要求「只保留三个模式卡片」，
  // 背后透出半截外壳反而像加载出错的残影）。z-50 与首启问答同层。
  return (
    <div
      className="fixed inset-0 z-50 flex flex-col items-center justify-center bg-background p-6"
      role="dialog"
      aria-modal="true"
      aria-label="选择使用方式"
    >
      <div className="w-full max-w-4xl text-center">
        <p className="font-mono text-xs uppercase tracking-widest text-primary">今天怎么用？</p>
        <h2 className="mt-3 text-3xl font-semibold text-foreground">选择一个使用方式</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          选完直接进入对应页面，能力会按场景自动启用；选错了随时能在左侧「使用模式」里改。
        </p>

        <div className="mt-10 grid gap-4 sm:grid-cols-3">
          {scenes.map(({ id, plugins }) => {
            const meta = SCENE_META[id]
            const Icon = meta.icon
            const active = ready && catalogState.catalog.preset === id
            return (
              <button
                key={id}
                type="button"
                disabled={switching !== null}
                onClick={() => void pick(id)}
                title={`启用 ${plugins.length} 项能力，进入「${meta.label}」`}
                className={cn(
                  "group flex flex-col items-center gap-3 rounded-3xl border bg-card px-6 py-8 text-center transition hover:-translate-y-1 hover:border-primary hover:shadow-xl disabled:opacity-60",
                  active ? "border-primary/60 ring-1 ring-primary/40" : "border-border",
                )}
              >
                <span className="flex h-14 w-14 items-center justify-center rounded-2xl border border-primary/40 bg-primary/10 text-primary">
                  <Icon className="h-7 w-7" />
                </span>
                <span className="flex items-center gap-1.5 text-base font-semibold text-card-foreground">
                  {meta.label}
                  {active ? (
                    <span className="flex items-center gap-0.5 text-[11px] font-normal text-primary">
                      <Check className="h-3.5 w-3.5" /> 当前
                    </span>
                  ) : null}
                </span>
                <span className="text-xs leading-5 text-muted-foreground">{meta.hint}</span>
                <span className="mt-2 flex items-center gap-1 text-sm text-primary">
                  进入 <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                </span>
              </button>
            )
          })}
          {!ready ? (
            <p className="col-span-full py-8 text-center text-sm text-muted-foreground">
              正在读取能力清单…
            </p>
          ) : null}
        </div>

        {switching ? (
          <p className="mt-6 flex items-center justify-center gap-2 text-sm text-primary" role="status">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在切换到「{SCENE_META[switching].label}」并重启服务，马上就好…
          </p>
        ) : null}

        <div className="mt-10 flex flex-wrap items-center justify-center gap-6">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={dontAsk}
              disabled={switching !== null}
              onChange={(e) => setDontAsk(e.target.checked)}
              className="h-3.5 w-3.5 accent-[var(--primary)]"
            />
            记住我的选择，下次不再询问
          </label>
          <button
            type="button"
            onClick={finish}
            disabled={switching !== null}
            className="rounded-md border border-border px-4 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-50"
          >
            直接进入
          </button>
        </div>
      </div>
    </div>
  )
}
