/**
 * 启动时的**全屏使用方式选择**（2026-10-03）。
 *
 * 与现有两个启动组件的分工 —— 三个都是「选完就走」，但维度不同：
 * - `UseCasePicker`（仅首次）：问「你**长期**用它干嘛」→ 影响首屏裁剪；
 *   首次启动时它先弹，本组件让位（见 shouldShowModeChooser）。
 * - `FirstLaunchGuide`（仅首次）：问「你想先干什么」→ 选完跳页。
 * - 本组件（**只出现一次**，2026-10-15 改）：问「要用哪种方式」→ 套用场景套餐
 *   + 跳到入口页。选错随时能改：侧栏「使用模式」块就是同一件事的常驻版。
 *
 * 为什么首次启动不弹：首次已经有 UseCasePicker 占住全屏问答位，再叠一个
 * 等于连问三句 —— 第二次打开再问才自然。
 *
 * ★ 2026-10-15 用户拍板：**它只出现一次**。原来每次启动都拦一屏，而卡片点下去是
 * 「整套重设」—— 这一屏就是误点最多的地方。现在两个前提都变了：
 *   ① 卡片默认**只增不减**（不关掉你手动开的能力，见 `applyPluginPreset`）；
 *   ② 交互过哪怕一次（选卡片 / 直接进入）就落 `vm.modechooser.dontask`，以后不再弹，
 *      想换模式从侧栏切。
 * 要「收窄到只剩这个套餐」走卡片下面那个显式的「严格套用（会关掉 N 项）」+ 确认。
 */
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router"
import { AlertTriangle, ArrowRight, Check, Loader2 } from "lucide-react"
import { applyPluginPreset } from "@/api/client"
import { restartBackend } from "@/lib/electron"
import {
  MODE_SUPPRESS_ONCE_KEY as SUPPRESS_KEY,
  capabilitiesLostBy,
  describeLost,
} from "@/lib/modeSwitch"
import { usePluginCatalog } from "@/lib/pluginRoutes"
import { hasBeenAsked } from "@/lib/useProfile"
import { cn } from "@/lib/utils"
import { SCENE_META } from "@/components/layout/ModeSwitcher"

const DONT_ASK_KEY = "vm.modechooser.dontask"
/** 模式切换要**重启后端**才生效（路由/插件集在启动时挂载）——重载后由本组件消费、跳到目标页。 */
const PENDING_ENTRY_KEY = "vm.pendingEntry"
/** 重载后一次性不弹选择页（刚选完模式再弹一次等于骚扰）。sessionStorage：关掉应用就失效。
 *  键名/写法统一走 `lib/modeSwitch.ts` —— 能力面板与侧栏的「立即重启」也往这里写，
 *  两处各写一份字面量就会「改一处忘一处」。 */

export function shouldShowModeChooser(): boolean {
  try {
    return hasBeenAsked() && localStorage.getItem(DONT_ASK_KEY) !== "1"
  } catch {
    return false
  }
}

/**
 * 落「问过了」标记 —— 本页从此不再出现（2026-10-15 起它只出现一次）。
 *
 * 三个出口（选卡片 / 严格套用 / 直接进入）都要调它：漏掉哪一个，用户就会从那一个
 * 出口回来后被再拦一次。「记住我的选择」那个勾选框因此删了 —— 现在不是选项，是语义。
 * 吞异常：没有 storage 时宁可多弹一次，也不能让按钮炸掉（同 `suppressModeChooserOnce`）。
 */
function rememberAsked(): void {
  try {
    localStorage.setItem(DONT_ASK_KEY, "1")
  } catch {
    /* 没有 storage 就算了 */
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
  const [switching, setSwitching] = useState<string | null>(null)
  /** 待确认的**严格套用**目标：只有这条路会关掉东西（2026-10-15） */
  const [confirmId, setConfirmId] = useState<string | null>(null)
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

  /** 出口：什么都不套用，只是关掉。★ 三个出口都落「问过了」—— 本页只出现一次。 */
  const finish = useCallback(() => {
    rememberAsked()
    setOpen(false)
  }, [])

  /**
   * 2026-10-03 修 bug：原先「发射后不管」地套套餐就跳页，但插件集与路由是
   * **后端启动时挂载的** —— 没重启，目标页路由根本不存在（被 `*` 兜底弹回首页），
   * catalog 缓存也没刷新（侧栏/顶栏还显示旧模式）。现在换成：
   * 套套餐 → 记下目标页 → 重启后端 → 整页重载（catalog 全新、路由齐全）→ 落到目标页。
   * 已是当前模式则不折腾，直接跳页。
   */
  /**
   * 真正执行套用。
   *
   * `strict=false`（默认）：只增不减 —— 后端只补齐、不关任何东西，所以可以直接走。
   * `strict=true`：显式收窄，会关掉不在套餐里的能力（只有确认后才调到这里）。
   */
  const doPick = useCallback(
    async (id: string, strict: boolean) => {
      const cat = catalogState.status === "ready" ? catalogState.catalog : null
      const entry = MODE_ENTRY[id] ?? "/home"
      if (!cat || cat.preset === id) {
        rememberAsked()
        setOpen(false)
        navigate(entry)
        return
      }
      setConfirmId(null)
      setSwitching(id)
      try {
        await applyPluginPreset(id, { strict })
      } catch {
        /* 失败也继续重启：重载后以清单为准，套没套上界面上看得到（侧栏高亮不骗人） */
      }
      localStorage.setItem(PENDING_ENTRY_KEY, entry)
      sessionStorage.setItem(SUPPRESS_KEY, "1")
      rememberAsked()
      try {
        await restartBackend()
      } catch {
        /* 纯网页调试没有重启通道 —— 重载后模式可能没换上，可从侧栏再切 */
      }
      location.reload()
    },
    [catalogState, navigate],
  )

  /**
   * 点卡片的入口：**只增不减**（2026-10-15）—— 不关你手动开的能力，所以不再拦确认。
   *
   * 原来这里会先算 `capabilitiesLostBy` 再弹一条「会关掉 N 项」的确认；现在这套
   * 逻辑只服务卡片下面那个**显式**的「严格套用」（`requestStrict`）。
   */
  const pick = useCallback(
    (id: string) => {
      void doPick(id, false)
    },
    [doPick],
  )

  /** 「严格套用」的入口：这才是会关掉 N 项的那个动作，先问一句。 */
  const requestStrict = useCallback((id: string) => {
    setConfirmId(id)
  }, [])

  // Esc = 直接进入（与 UseCasePicker 同款键盘出口约定）；切换中不给 Esc 出口
  useEffect(() => {
    if (!open || switching) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // 确认态下 Esc = 取消这一下（不是「直接进入」）——否则想撤回会顺手把弹窗也关掉
        if (confirmId) {
          setConfirmId(null)
          return
        }
        finish()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, switching, confirmId, finish])

  if (!open) return null

  const ready = catalogState.status === "ready"
  const scenes = ready ? catalogState.catalog.presets.filter((p) => p.id in SCENE_META) : []
  const confirmLost = confirmId
    ? capabilitiesLostBy(ready ? catalogState.catalog : null, confirmId)
    : []
  const confirmMeta = confirmId ? SCENE_META[confirmId] : null

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
          选完直接进入对应页面：能力按场景<strong className="font-medium text-foreground">补齐</strong>，
          你手动开过的一项都不会被关掉。想「只剩这一个场景」用下面的「严格套用」。
          选错了随时能在左侧「使用模式」里改。
        </p>

        <div className="mt-10 grid gap-4 sm:grid-cols-3">
          {scenes.map(({ id, plugins }) => {
            const meta = SCENE_META[id]
            const Icon = meta.icon
            const active = ready && catalogState.catalog.preset === id
            // 严格套用会关掉的那几项（默认点卡片不看它 —— 默认一项都不关）
            const lost = ready ? capabilitiesLostBy(catalogState.catalog, id) : []
            return (
              <div key={id}>
              <button
                type="button"
                disabled={switching !== null}
                onClick={() => pick(id)}
                title={`启用 ${plugins.length} 项能力，进入「${meta.label}」（不会关闭你手动开启的能力）`}
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
              {/* 收窄是**显式**动作（2026-10-15）：它才是会关掉手动开启项的那一下，
                  所以做成卡片之外的一行、点了还要确认。不会关东西的场景不显示它。 */}
              {lost.length > 0 ? (
                <button
                  type="button"
                  disabled={switching !== null}
                  onClick={() => requestStrict(id)}
                  title={`严格套用「${meta.label}」：关闭不在该场景里的 ${lost.length} 项（含你手动开启的）—— 点一下会先让你确认`}
                  className="mt-2 w-full rounded-md px-2 py-1 text-[11px] text-muted-foreground transition hover:text-foreground disabled:opacity-50"
                >
                  严格套用（会关掉 {lost.length} 项）
                </button>
              ) : null}
              </div>
            )
          })}
          {!ready ? (
            <p className="col-span-full py-8 text-center text-sm text-muted-foreground">
              正在读取能力清单…
            </p>
          ) : null}
        </div>

        {/* 会关掉东西才出现的一次性确认。这一屏是「随手一点」发生得最多的地方，
            而卡片点下去是**整套重设** —— 把将关闭的几项点名，而不是只问「确定吗？」 */}
        {confirmId && confirmMeta ? (
          <div
            role="alert"
            className="mx-auto mt-8 max-w-xl rounded-2xl border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-left"
          >
            <p className="flex items-start gap-2 text-sm leading-6 text-amber-700 dark:text-amber-400">
              <AlertTriangle className="mt-1 h-4 w-4 shrink-0" aria-hidden="true" />
              <span>
                严格套用「{confirmMeta.label}」会<strong className="font-medium">关掉</strong>你手动开启的{" "}
                {confirmLost.length} 项能力：{describeLost(confirmLost.map((p) => p.name))}
              </span>
            </p>
            <div className="mt-3 flex items-center justify-end gap-2">
              <button
                type="button"
                disabled={switching !== null}
                onClick={() => setConfirmId(null)}
                className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                disabled={switching !== null}
                onClick={() => void doPick(confirmId, true)}
                className="rounded-md border border-amber-500/50 bg-amber-500/15 px-3 py-1 text-xs font-medium text-amber-700 transition hover:bg-amber-500/25 disabled:opacity-60 dark:text-amber-400"
              >
                确认严格套用并重启
              </button>
            </div>
          </div>
        ) : null}

        {switching ? (
          <p className="mt-6 flex items-center justify-center gap-2 text-sm text-primary" role="status">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在切换到「{SCENE_META[switching].label}」并重启服务，马上就好…
          </p>
        ) : null}

        <div className="mt-10 flex flex-wrap items-center justify-center gap-6">
          {/* 勾选框删了（2026-10-15）：现在不是选项而是语义 —— 这页只出现一次。 */}
          <span className="text-xs text-muted-foreground">
            这页只在第一次出现；以后随时可在左侧「使用模式」里切换
          </span>
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
