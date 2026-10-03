/**
 * 侧栏「使用模式」切换块（2026-10-03）。
 *
 * 为什么存在：三个场景套餐（微信变身 / 声音保存 / 游戏变身）原本只住在
 * 能力面板（设置抽屉）的套餐行里 —— 用户反馈「找不到在哪里」。模式是产品级
 * 的入口，值得常驻侧栏，而不是藏在二级抽屉的一排按钮里。
 *
 * 数据源：套餐清单仍由后端 /api/plugins 驱动（单一真相源），这里只按
 * 场景 id 过滤 + 配图标和一句话说明。当前模式 = catalog.preset（后端判定，
 * "custom" 表示用户逐项改过、不匹配任何套餐）。
 */
import { useCallback, useEffect, useState } from "react"
import { Check, Gamepad2, GraduationCap, Loader2, MessageCircle, RotateCcw } from "lucide-react"
import { applyPluginPreset, getPlugins } from "@/api/client"
import { restartBackend } from "@/lib/electron"
import { cn } from "@/lib/utils"

/** 场景套餐的本地配置：图标 + 一句话说明（标签以后端为准）。
 *  不在表里的套餐（light/full/…）不进侧栏 —— 侧栏只留「按用户是谁」分的三个场景。 */
const SCENE_META: Record<string, { icon: typeof Gamepad2; hint: string }> = {
  wechat: { icon: MessageCircle, hint: "聊天变声 · 微信发送" },
  preserve: { icon: GraduationCap, hint: "录下声音 · 护嗓讲课" },
  game: { icon: Gamepad2, hint: "游戏直播 · 实时变声" },
}

type Catalog = Awaited<ReturnType<typeof getPlugins>>

export function ModeSwitcher() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [needRestart, setNeedRestart] = useState(false)
  const [error, setError] = useState("")

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

  const apply = useCallback(async (id: string) => {
    setBusy(id)
    setError("")
    try {
      await applyPluginPreset(id)
      setNeedRestart(true)
      // 套餐动的是整组；本地乐观高亮，重启后以目录判定为准
      setCatalog((c) => (c ? { ...c, preset: id } : c))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }, [])

  const restart = useCallback(async () => {
    setBusy("restart")
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
          return (
            <button
              key={p.id}
              type="button"
              disabled={busy !== null}
              title={`切换到「${p.label}」：启用 ${p.plugins.length} 项能力${active ? "（当前）" : ""}，重启应用后生效`}
              onClick={() => void apply(p.id)}
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
          )
        })}
      </div>
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
      {error ? <p className="mt-2 px-1 text-[11px] text-red-500">{error}</p> : null}
    </div>
  )
}
