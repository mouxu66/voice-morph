import { useEffect, useState } from "react"
import { ArrowRight, Gamepad2, Keyboard, Sparkles, Wrench, X } from "lucide-react"
import { USES, getUseCase, hasBeenAsked, setUseCase, type UseCase } from "@/lib/useProfile"

/**
 * 首次启动的**用途问答**（一问一答，不是教程）。
 *
 * 与 `FirstLaunchGuide` 的分工
 * ---------------------------
 * 两者都只弹一次，但问的是**不同维度**，所以会串行出现、不会同时弹：
 * - `UseCasePicker`（本文件）：问「**你用它干嘛**」→ 决定首屏裁剪（场景维度）
 * - `FirstLaunchGuide`：问「**你想先干什么**」→ 决定跳到哪个页面（意图维度）
 *
 * 为什么不能合并成一个：场景是"长期偏好"（存起来、随时可改），意图是"本次动作"
 * （选完就走）。混成一个会让"我想先玩玩"被误解成"我的长期用途是玩"。
 *
 * 三条实现约束（都是踩过的坑的类型）
 * --------------------------------
 * 1. **每个选项都能关**。调研里点名「强制走完不可跳过的教程」会推高 bounce rate，
 *    所以右上角有 ×、右下角有「先逛逛」，两条都等价于"不回答"，同样落盘。
 * 2. **Esc 能关**。模态框不给键盘出口是硬伤。
 * 3. **`onPick` 回调不直接 navigate**。用途只回答"之后先给我看哪个"，
 *    不该替用户跳页 —— 跳页交给 FirstLaunchGuide。这样用户点完用途仍停在首页，
 *    看到首页已按他的答案重排，反馈是可见的。
 */
export function UseCasePicker({ onPicked }: { onPicked?: (id: UseCase) => void }) {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    // 已经问过就不再问（`hasBeenAsked` 读不到时按问过处理，见 useProfile 注释）
    if (!hasBeenAsked()) setOpen(true)
    const onReplay = () => setOpen(true)
    window.addEventListener("replay-use-picker", onReplay)
    return () => window.removeEventListener("replay-use-picker", onReplay)
  }, [])

  const close = (id: UseCase) => {
    setUseCase(id)
    setOpen(false)
    onPicked?.(id)
  }

  // Esc = 「先逛逛」（同样落盘，否则下次又弹）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close("skip")
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
    // close 用不到 state 以外的依赖（setUseCase/onPicked 都是稳定引用）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!open) return null

  // 图标按 id 取。放这里而不是 USES 里，是因为 USES 在 lib/ 下、
  // 不该把 lucide 组件带进非 UI 模块（tree-shaking 与分层都会乱）。
  const ICON: Record<string, typeof Gamepad2> = {
    game: Gamepad2,
    wechat: Keyboard,
    dubbing: Wrench,
    skip: Sparkles,
  }

  const current = getUseCase()

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/75 p-4 backdrop-blur-xl"
      role="dialog"
      aria-modal="true"
      aria-label="选择用途"
    >
      <div className="w-full max-w-2xl overflow-hidden rounded-3xl border border-border bg-card/95 shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-border px-8 pb-5 pt-8">
          <div>
            <p className="font-mono text-xs uppercase tracking-widest text-primary">先问一句</p>
            <h2 className="mt-2 text-2xl font-semibold text-foreground">你打算用它做什么？</h2>
            <p className="mt-1.5 text-sm text-muted-foreground">
              选一个，首页只显示这条路上要用的东西；选错了随时能改，能力一个都不会少。
            </p>
          </div>
          <button
            type="button"
            onClick={() => close("skip")}
            aria-label="跳过"
            className="rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="grid gap-3 px-8 py-6 sm:grid-cols-2">
          {USES.map(({ id, name, hint }) => {
            const Icon = ICON[id] ?? Sparkles
            const active = current === id
            return (
              <button
                key={id}
                type="button"
                onClick={() => close(id)}
                className={
                  "group flex items-start gap-3 rounded-2xl border bg-background/60 p-4 text-left transition hover:-translate-y-0.5 hover:border-primary hover:shadow-md " +
                  (active ? "border-primary/60" : "border-border")
                }
              >
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-primary/40 bg-primary/10 text-primary">
                  <Icon className="h-5 w-5" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-semibold text-card-foreground">{name}</span>
                  <span className="mt-1 block text-xs leading-5 text-muted-foreground">{hint}</span>
                </span>
                <ArrowRight className="mt-2 h-4 w-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-primary" />
              </button>
            )
          })}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-8 py-4">
          <p className="text-[11px] text-muted-foreground">
            以后想改：设置 → 「我的用途」。选「我还没想好」就是全都不裁剪。
          </p>
          <button
            type="button"
            onClick={() => close("skip")}
            className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            先逛逛
          </button>
        </div>
      </div>
    </div>
  )
}

/** 供设置面板复用的重播触发器（与 FirstLaunchGuide 同款事件约定）。 */
export function replayUsePicker(): void {
  window.dispatchEvent(new Event("replay-use-picker"))
}
