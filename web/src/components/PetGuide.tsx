import { useEffect, useRef, useState } from "react"
import { cn } from "@/lib/utils"
import { GREETING, GUIDES, lineMs, type PageGuide } from "@/lib/petGuide"

const SPRITES: Record<string, string> = {
  idle: new URL("../../electron/pet/svg/idle.webp", import.meta.url).href,
  listen: new URL("../../electron/pet/svg/idle-look.webp", import.meta.url).href,
  think: new URL("../../electron/pet/svg/thinking.webp", import.meta.url).href,
  play: new URL("../../electron/pet/svg/conducting.webp", import.meta.url).href,
  build: new URL("../../electron/pet/svg/building.webp", import.meta.url).href,
  error: new URL("../../electron/pet/svg/error.webp", import.meta.url).href,
}

const SPRITE_ANIM: Record<string, { frames: number; dur: number }> = {
  idle: { frames: 37, dur: 3.0 },
  listen: { frames: 37, dur: 3.0 },
  think: { frames: 37, dur: 3.0 },
  play: { frames: 22, dur: 1.8 },
  build: { frames: 16, dur: 1.4 },
  error: { frames: 37, dur: 3.0 },
}

const MOTIONS = ["pop", "wave", "lean", "nod", "sway", "jump", "twirl",
  "shake", "float", "wiggle", "flip", "bounce", "dance", "ear", "work"]

/**
 * 页面内导览桌宠：固定在主内容区右上角，切页时主动蹦出来介绍当前页，
 * 讲完后缩成一个小头像，点一下可重播。
 */
export function PetGuide({ page, enabled }: { page: string; enabled: boolean }) {
  const [visible, setVisible] = useState(false)
  const [expanded, setExpanded] = useState(true)
  const [lines, setLines] = useState<string[]>([])
  const [current, setCurrent] = useState(0)
  const [displayText, setDisplayText] = useState("")
  const seen = useRef<Set<string>>(new Set())
  const greeted = useRef(false)
  const timers = useRef<number[]>([])

  // 切页 / 开关变化：重新准备文案
  useEffect(() => {
    // 清掉旧的定时器，避免上一页台词串到下一页
    timers.current.forEach(clearTimeout)
    timers.current = []
    if (typingRef.current != null) clearInterval(typingRef.current)
    typingRef.current = null

    const g = GUIDES[page]
    if (!g || !enabled) {
      setVisible(false)
      return
    }

    const ls = g.lines.slice()
    if (!greeted.current) {
      ls.unshift(GREETING)
      greeted.current = true
    } else if (seen.current.has(page)) {
      ls.length = 1 // 熟面孔只提示一句
    }
    seen.current.add(page)

    setLines(ls)
    setCurrent(0)
    setVisible(true)
    setExpanded(true)
  }, [page, enabled])

  const typingRef = useRef<number | null>(null)

  // 当前句打字机效果
  // 当前句：整句直接显示，停留阅读时长后切下一句
  useEffect(() => {
    if (!lines.length) return
    const text = lines[current]
    if (!text) return

    setDisplayText(text)

    const t = window.setTimeout(() => {
      if (current < lines.length - 1) {
        setCurrent((c) => c + 1)
      } else {
        // 全部讲完，4s 后缩成小头像
        const t2 = window.setTimeout(() => setExpanded(false), 4000)
        timers.current.push(t2)
      }
    }, lineMs(text))
    timers.current.push(t)

    return () => window.clearTimeout(t)
  }, [lines, current])

  // 设置面板「让桌宠再讲一遍本页」
  useEffect(() => {
    const replay = () => {
      setExpanded(true)
      setCurrent(0)
    }
    window.addEventListener("replay-pet-guide" as any, replay)
    return () => window.removeEventListener("replay-pet-guide" as any, replay)
  }, [])

  const guide: PageGuide | undefined = GUIDES[page]
  if (!visible || !guide) return null

  const anim = SPRITE_ANIM[guide.action] ?? SPRITE_ANIM.idle
  // 收起时按钮只有 64px，精灵图也缩到 64px，配合 overflow-hidden 避免角色穿出圆形头像
  const spriteSize = expanded ? 96 : 64
  const spriteStyle: React.CSSProperties = {
    backgroundImage: `url("${SPRITES[guide.action] ?? SPRITES.idle}")`,
    backgroundRepeat: "no-repeat",
    backgroundSize: `${anim.frames * spriteSize}px ${spriteSize}px`,
    ["--steps-total" as string]: `${anim.frames * spriteSize}px`,
    width: spriteSize,
    height: spriteSize,
    willChange: "background-position",
    transform: "translateZ(0)",
    animation: `pet-steps ${anim.dur}s steps(${anim.frames}) infinite`,
  }

  return (
    <div
      className={cn(
        "fixed right-4 top-24 z-30 flex items-end gap-3 transition-all duration-500 ease-out lg:right-8",
        expanded ? "translate-x-0 opacity-100" : "translate-x-2 opacity-90"
      )}
    >
      {expanded && (
        <div className="group relative max-w-[260px] rounded-2xl border border-border bg-card/95 p-4 shadow-xl backdrop-blur-md">
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="absolute right-2 top-1 text-[10px] text-muted-foreground opacity-0 transition hover:text-foreground group-hover:opacity-100"
            aria-label="收起"
          >
            收起
          </button>
          <p className="pr-5 text-sm font-semibold text-foreground">{guide.title}</p>
          <p className="mt-1 min-h-[1.5em] text-sm leading-relaxed text-card-foreground">
            {displayText}
          </p>
          <div className="mt-3 flex items-center justify-between">
            <div className="flex gap-1.5">
              {lines.map((_, idx) => (
                <span
                  key={idx}
                  className={cn(
                    "h-1.5 w-1.5 rounded-full transition-colors",
                    idx === current ? "bg-primary" : "bg-primary/25"
                  )}
                />
              ))}
            </div>
            <button
              type="button"
              onClick={() => {
                setExpanded(false)
                timers.current.forEach(clearTimeout)
                timers.current = []
              }}
              className="text-[10px] text-muted-foreground transition hover:text-foreground"
            >
              跳过
            </button>
          </div>
        </div>
      )}

      <button
        type="button"
        onClick={() => setExpanded(true)}
        className={cn(
          "relative shrink-0 overflow-hidden rounded-full border border-border bg-card shadow-lg transition hover:shadow-xl",
          expanded ? "h-24 w-24" : "h-16 w-16"
        )}
        title="导览小助手"
      >
        <div
          className={cn(
            "absolute inset-0 m-auto transition-transform",
            guide.motion && MOTIONS.includes(guide.motion) ? `m-${guide.motion}` : ""
          )}
          style={spriteStyle}
        />
      </button>
    </div>
  )
}
