/**
 * 侧栏「使用模式」切换块（2026-10-03）。
 *
 * 为什么存在：三个场景套餐（微信变声 / 声音保存 / 游戏变声）原本只住在
 * 能力面板（设置抽屉）的套餐行里 —— 用户反馈「找不到在哪里」。模式是产品级
 * 的入口，值得常驻侧栏，而不是藏在二级抽屉的一排按钮里。
 *
 * 数据源：套餐清单仍由后端 /api/plugins 驱动（单一真相源），这里只按
 * 场景 id 过滤 + 配图标和一句话说明。当前模式 = catalog.preset（后端判定，
 * "custom" 表示用户逐项改过、不匹配任何套餐）。
 *
 * ---------------------------------------------------------------- 2026-10-08：先问一句
 *
 * 套餐当时是**整套重设**：切过去 = 把不在套餐里的能力全部关掉。这话在「一开始就是
 * 某个模式」时无感，但用户如果在能力面板里逐项开过（`preset` 变成 `custom`），
 * 这里点一下就会把那些开关全收走 —— 而按钮上写着「切换到…」，读起来像
 * 「我要用这个模式」，不像「我要关掉 17 项能力」。
 *
 * 当时的处理：**只在真的会关掉东西时**弹一条内联确认（把要关的几项点名）。
 *
 * ---------------------------------------------------------------- 2026-10-15：默认不再关东西
 *
 * 确认条还是不够 —— 用户原话「切换模式会把我自己手动打开的功能给关掉 …… 不能
 * 这么搞」。所以语义改了：
 *
 *   · **点模式 = 只增不减**（`applyPluginPreset(id)` 默认 `strict=false`）：只把该
 *     套餐点名的能力补齐，**一项也不关**。因此默认路径既不确认也不弹小字 ——
 *     没有东西会被夺走，再问就是白问。
 *   · **收窄 = 显式动作**：卡片下面多一行「严格套用（会关掉 N 项）」，点了才进确认条，
 *     确认后走 `applyPluginPreset(id, { strict: true })`（后端才整套重设）。
 *   · **切换后不做乐观高亮**：只增不减下 `catalog.preset` 很可能变成 `custom`
 *     （因为保留了你开的项）—— 侧栏宁可如实显示「当前不是某个套餐」，也不说一个
 *     后端不认的模式名（同 `lib/modeSwitch.ts` 头注释的判据纪律）。
 *     回执由响应里的 `kept` 说出来：「已套用「游戏变声」· 保留你手动开启的 4 项」。
 */
import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, Check, Gamepad2, GraduationCap, Loader2, MessageCircle, RotateCcw } from "lucide-react"
import { applyPluginPreset, getPlugins } from "@/api/client"
import { restartBackend } from "@/lib/electron"
import { capabilitiesLostBy, describeLost, suppressModeChooserOnce } from "@/lib/modeSwitch"
import { cn } from "@/lib/utils"

/** 场景套餐的本地配置：图标 + 一句话说明（标签以后端为准）。
 *  不在表里的套餐（light/full/…）不进侧栏 —— 侧栏只留「按用户是谁」分的三个场景。
 *  `label` 是本地兜底：顶栏徽章（AppChrome）在卡片点击后立即显示，等不及后端清单刷新。 */
export const SCENE_META: Record<string, { icon: typeof Gamepad2; label: string; hint: string }> = {
  wechat: { icon: MessageCircle, label: "微信变声", hint: "聊天变声 · 微信发送" },
  preserve: { icon: GraduationCap, label: "声音保存", hint: "录下声音 · 护嗓讲课" },
  game: { icon: Gamepad2, label: "游戏变声", hint: "游戏直播 · 实时变声" },
}

type Catalog = Awaited<ReturnType<typeof getPlugins>>

export function ModeSwitcher() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [needRestart, setNeedRestart] = useState(false)
  const [error, setError] = useState("")
  /** 待确认的**严格套用**目标：只有这条路会关掉东西，所以只有它先问一句。 */
  const [confirmId, setConfirmId] = useState<string | null>(null)
  /** 切换后的一句话回执（「保留你手动开启的 N 项」）—— 用户此刻最想知道的就是这个。 */
  const [notice, setNotice] = useState("")

  useEffect(() => {
    let alive = true
    getPlugins()
      .then((d) => {
        if (alive) setCatalog(d)
      })
      .catch(() => {
        /* 后端没起：这块静默隐藏 —— 侧栏不该因为一个增强块报错 */
      })
    return () => {
      alive = false
    }
  }, [])

  const labelOf = useCallback(
    (id: string) => catalog?.presets.find((p) => p.id === id)?.label ?? SCENE_META[id]?.label ?? id,
    [catalog],
  )

  const refresh = useCallback(async () => {
    try {
      setCatalog(await getPlugins())
    } catch {
      /* 刷新失败就保持旧目录：不假装成功，也不把侧栏清空 */
    }
  }, [])

  const apply = useCallback(
    async (id: string, strict: boolean) => {
      setBusy(id)
      setError("")
      setNotice("")
      setConfirmId(null)
      try {
        const res = await applyPluginPreset(id, { strict })
        setNeedRestart(true)
        // `kept` 用可选链读：老后端（或单测里的窄 mock）没有这个字段时，
        // 这里**不该炸** —— 回执少一句是小事，把切换整个吞掉是大事。
        const kept = res.kept?.length ?? 0
        setNotice(
          strict
            ? `已严格套用「${labelOf(id)}」：不在套餐里的能力已关闭`
            : kept > 0
              ? `已套用「${labelOf(id)}」· 保留你手动开启的 ${kept} 项`
              : `已套用「${labelOf(id)}」`,
        )
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setBusy(null)
      }
    },
    [labelOf, refresh],
  )

  /** 点模式的入口：**只增不减**，不会夺走任何东西 —— 所以不问、也不拦（2026-10-15）。 */
  const request = useCallback(
    (id: string) => {
      void apply(id, false)
    },
    [apply],
  )

  /** 「严格套用」的入口：这才是会关掉 N 项的那个动作，先问一句。 */
  const requestStrict = useCallback((id: string) => {
    setError("")
    setNotice("")
    setConfirmId(id)
  }, [])

  const restart = useCallback(async () => {
    setBusy("restart")
    // ★ 关键：这次重载是「用户刚调完设置」，不该再被模式选择页拦一次 ——
    //   否则用户点「立即重启」，回来迎面一个「今天怎么用？」，随手一点就把设置全废了。
    suppressModeChooserOnce()
    try {
      await restartBackend()
    } catch {
      /* 桌面端通道不在（纯网页调试）—— 下面无论如何都刷新，让新清单生效 */
    } finally {
      location.reload()
    }
  }, [])

  const scenes = (catalog?.presets ?? []).filter((p) => p.id in SCENE_META)
  // 当前套餐不在场景三件套里（light/full/custom…）→ 没有高亮项，全部平铺展示
  const current = catalog?.preset ?? null
  const confirmLost = confirmId ? capabilitiesLostBy(catalog, confirmId) : []
  const confirmLabel = catalog?.presets.find((p) => p.id === confirmId)?.label ?? confirmId

  if (scenes.length === 0) return null

  return (
    <div className="rounded-xl border border-border bg-card p-2.5">
      <div className="mb-1.5 px-1 text-[11px] font-medium text-muted-foreground">使用模式</div>
      <div className="space-y-1">
        {scenes.map((p) => {
          const meta = SCENE_META[p.id]
          const Icon = meta.icon
          const active = current === p.id
          const isBusy = busy === p.id
          // 严格套用会关掉的那几项（默认切换不看它 —— 默认一项都不关）
          const lost = capabilitiesLostBy(catalog, p.id)
          return (
            <div key={p.id}>
              <button
                type="button"
                disabled={busy !== null}
                title={`切换到「${p.label}」：启用 ${p.plugins.length} 项能力${active ? "（当前）" : ""}，不会关闭你手动开启的能力，重启应用后生效`}
                onClick={() => request(p.id)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs transition",
                  active
                    ? "border border-primary/40 bg-primary/10 text-foreground"
                    : "border border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                  busy !== null && "opacity-60",
                )}
              >
                {isBusy ? (
                  <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : (
                  <Icon className="h-3.5 w-3.5 shrink-0" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{p.label}</span>
                  <span className="block truncate text-[10px] opacity-70">{meta.hint}</span>
                </span>
                {active ? <Check className="h-3.5 w-3.5 shrink-0 text-primary" /> : null}
              </button>
              {/* 收窄是**显式**动作（2026-10-15）：它才是会关掉手动开启项的那一下，
                  所以只在这里出现、且点了要确认。不会关东西的模式不显示这行。 */}
              {lost.length > 0 ? (
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => requestStrict(p.id)}
                  title={`严格套用「${p.label}」：关闭不在该套餐里的 ${lost.length} 项（含你手动开启的）—— 点一下会先让你确认`}
                  className="mt-0.5 w-full rounded-md px-2 py-0.5 text-left text-[10px] text-muted-foreground/80 transition hover:text-foreground disabled:opacity-60"
                >
                  严格套用（会关掉 {lost.length} 项）
                </button>
              ) : null}
            </div>
          )
        })}
      </div>
      {/* 严格套用才出现的一次性确认：把「将被收走的那几项」点名，而不是只问
          「确定吗？」—— 用户此刻要判断的恰恰是「掉的是不是我想要的」。 */}
      {confirmId ? (
        <div
          role="alert"
          className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-2 py-1.5 text-[11px] leading-5"
        >
          <p className="flex items-start gap-1 text-amber-700 dark:text-amber-400">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
            <span>
              严格套用「{confirmLabel}」会<strong className="font-medium">关掉</strong>你手动开启的{" "}
              {confirmLost.length} 项：{describeLost(confirmLost.map((p) => p.name))}
            </span>
          </p>
          <div className="mt-1.5 flex items-center gap-2">
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => void apply(confirmId, true)}
              className="rounded-md border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 font-medium text-amber-700 transition hover:bg-amber-500/25 disabled:opacity-60 dark:text-amber-400"
            >
              确认严格套用
            </button>
            <button
              type="button"
              disabled={busy !== null}
              onClick={() => setConfirmId(null)}
              className="rounded-md border border-border px-2 py-0.5 text-muted-foreground transition hover:text-foreground disabled:opacity-60"
            >
              取消
            </button>
          </div>
        </div>
      ) : null}

      {needRestart ? (
        <div className="mt-2 flex items-center gap-1.5 rounded-md bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-600">
          <span className="min-w-0 flex-1">重启后生效</span>
          <button
            type="button"
            onClick={() => void restart()}
            disabled={busy !== null}
            className="flex shrink-0 items-center gap-1 rounded-md bg-amber-500/20 px-1.5 py-0.5 font-medium hover:bg-amber-500/30"
          >
            {busy === "restart" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <RotateCcw className="h-3 w-3" />
            )}
            立即重启
          </button>
        </div>
      ) : null}
      {notice ? <p className="mt-2 px-1 text-[11px] text-primary">{notice}</p> : null}
      {error ? <p className="mt-2 px-1 text-[11px] text-red-500">{error}</p> : null}
    </div>
  )
}
