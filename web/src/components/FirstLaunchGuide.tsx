import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { ArrowRight, Keyboard, Mic2, Sparkles, X } from "lucide-react"

const LS_KEY = "vm_first_launch_done"

/** 三条路的文案：只讲"怎么选"，不讲操作细节（细节在各自页面里） */
const ENTRANCES = [
  {
    to: "/home",
    icon: Sparkles,
    name: "我只想立刻玩",
    desc: "官方开源预置音色，点一下就能听；听中意了装上，开麦就能说话。",
    cta: "去挑音色",
  },
  {
    to: "/tts",
    icon: Keyboard,
    name: "我想让它读我打的话",
    desc: "打字它就说，先白嫖几版听听腔调，再决定要不要练专属的。",
    cta: "去输字变声",
  },
  {
    to: "/workshop",
    icon: Mic2,
    name: "想要专属我自己的声音",
    desc: "从 10 秒人声开始：采集、存档、训练，一步步练出你的嗓子。",
    cta: "去训练变声",
  },
]

/**
 * 首次启动引导：只在第一次打开时弹一次（localStorage 标记），
 * 三张入口卡对应首页三条主路，点任意一条直接进对应页面。
 * 设置 →「重播新手引导」通过 replay-first-launch 事件再次打开。
 */
export function FirstLaunchGuide() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)

  const close = () => {
    setOpen(false)
    try {
      localStorage.setItem(LS_KEY, "1")
    } catch {
      /* 隐私模式写不了，下次再弹也无妨 */
    }
  }
  const go = (to: string) => {
    close()
    navigate(to)
  }

  useEffect(() => {
    try {
      if (!localStorage.getItem(LS_KEY)) setOpen(true)
    } catch {
      /* 隐私模式读不了，跳过引导 */
    }
    const onReplay = () => setOpen(true)
    window.addEventListener("replay-first-launch", onReplay)
    return () => window.removeEventListener("replay-first-launch", onReplay)
  }, [])

  // Esc 关闭（与「先逛逛」等价，同样落盘标记）
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open])

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 p-4 backdrop-blur-xl"
      role="dialog"
      aria-modal="true"
      aria-label="首次启动引导"
    >
      <div className="w-full max-w-2xl overflow-hidden rounded-3xl border border-border bg-card/95 shadow-2xl">
        <div className="flex items-start justify-between gap-4 border-b border-border px-8 pb-5 pt-8">
          <div>
            <p className="font-mono text-xs uppercase tracking-widest text-primary">WELCOME · 首次启动</p>
            <h2 className="mt-2 text-2xl font-semibold text-foreground">三句话，选好怎么用</h2>
            <p className="mt-1.5 text-sm text-muted-foreground">
              变声工坊就三条路，选一条先玩起来；玩熟了随时可以换。
            </p>
          </div>
          <button
            type="button"
            onClick={close}
            aria-label="关闭引导"
            className="rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="grid gap-3 px-8 py-6 sm:grid-cols-3">
          {ENTRANCES.map(({ to, icon: Icon, name, desc, cta }) => (
            <button
              key={to}
              type="button"
              onClick={() => go(to)}
              className="group flex flex-col gap-3 rounded-2xl border border-border bg-background/60 p-5 text-left shadow-sm transition hover:-translate-y-0.5 hover:border-primary hover:shadow-lg"
            >
              <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-primary/40 bg-primary/10 text-primary">
                <Icon className="h-5 w-5" />
              </span>
              <span className="text-sm font-semibold text-card-foreground">{name}</span>
              <span className="text-xs leading-5 text-muted-foreground">{desc}</span>
              <span className="mt-auto flex items-center gap-1 text-xs font-medium text-primary">
                {cta} <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
              </span>
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-8 py-4">
          <p className="text-[11px] text-muted-foreground">以后想重看：设置 → 重播新手引导</p>
          <button
            type="button"
            onClick={close}
            className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            先逛逛
          </button>
        </div>
      </div>
    </div>
  )
}