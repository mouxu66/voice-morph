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
import { ArrowRight, Check } from "lucide-react"
import { applyPluginPreset } from "@/api/client"
import { usePluginCatalog } from "@/lib/pluginRoutes"
import { hasBeenAsked } from "@/lib/useProfile"
import { cn } from "@/lib/utils"
import { SCENE_META } from "@/components/layout/ModeSwitcher"

const DONT_ASK_KEY = "vm.modechooser.dontask"

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
  const { state: catalogState } = usePluginCatalog()
  const navigate = useNavigate()

  const finish = useCallback(() => {
    if (dontAsk) localStorage.setItem(DONT_ASK_KEY, "1")
    setOpen(false)
  }, [dontAsk])

  const pick = useCallback(
    (id: string) => {
      const cat = catalogState.status === "ready" ? catalogState.catalog : null
      // 套餐不匹配才套用；失败不拦跳页 —— 模式切换可以之后在侧栏补做
      if (cat && cat.preset !== id) applyPluginPreset(id).catch(() => {})
      finish()
      navigate(MODE_ENTRY[id] ?? "/home")
    },
    [catalogState, finish, navigate],
  )

  // Esc = 直接进入（与 UseCasePicker 同款键盘出口约定）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finish()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, finish])

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
                onClick={() => pick(id)}
                title={`启用 ${plugins.length} 项能力，进入「${meta.label}」`}
                className={cn(
                  "group flex flex-col items-center gap-3 rounded-3xl border bg-card px-6 py-8 text-center transition hover:-translate-y-1 hover:border-primary hover:shadow-xl",
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

        <div className="mt-10 flex flex-wrap items-center justify-center gap-6">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={dontAsk}
              onChange={(e) => setDontAsk(e.target.checked)}
              className="h-3.5 w-3.5 accent-[var(--primary)]"
            />
            记住我的选择，下次不再询问
          </label>
          <button
            type="button"
            onClick={finish}
            className="rounded-md border border-border px-4 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            直接进入
          </button>
        </div>
      </div>
    </div>
  )
}
