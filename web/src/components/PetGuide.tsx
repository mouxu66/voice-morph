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
 * 页面内导览桌宠：**展开态是文档流里的一条顶部横幅，收起态才 fixed 到右下角**。
 *
 * 位置不是随手定的，是被 2026-10-03 的遮挡实测逼出来的（详见 `docs/犯错档案-工程.md`）：
 * 之前两种摆法都错。
 *
 * ① 一直 `fixed` 贴在主内容区右上 —— 必然遮挡。`PageShell` 是 `max-w-[1240px]` 居中，
 *    1440px 视口下右侧只剩 16px 空隙，而展开态要占 ~400px（气泡 260 + 间距 12 +
 *    立绘 96 + 右边距 32）。实测盖住三页右侧栏的小节标题，最高 92%。
 * ② 改成给 `<main>` 加 `padding-right: 400px` 让位 —— 遮挡确实归零，但**每行文字
 *    都重新换行**，「我的音色」页从 6.7 屏涨到 8 屏。横向重排比遮挡本身更伤。
 *
 * 所以展开态干脆**不浮了**：回到文档流顶部，横幅自己占一行，把内容整体**往下**推。
 * 纵向位移不会让任何一行文字重新折行，右侧栏的小节标题也就永远不可能被压住。
 * 收起后横幅卸载、右下角只剩 64px 头像（那个尺寸实测无遮挡）。
 */

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
    window.addEventListener("replay-pet-guide", replay)
    return () => window.removeEventListener("replay-pet-guide", replay)
  }, [])

  const guide: PageGuide | undefined = GUIDES[page]
  const shown = visible && !!guide

  if (!shown || !guide) return null

  const anim = SPRITE_ANIM[guide.action] ?? SPRITE_ANIM.idle
  // 两种状态都用 64px：展开横幅里 96px 会把横幅撑得太高（内容被推得更下面），
  // 收起态本来就只有 64px。统一后 `spriteStyle` 只需算一次。
  const spriteSize = 64
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

  const avatar = (
    <button
      type="button"
      onClick={() => setExpanded(true)}
      className="relative shrink-0 overflow-hidden rounded-full border border-border bg-card shadow-lg transition hover:shadow-xl"
      style={{ width: spriteSize, height: spriteSize }}
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
  )

  // 展开态：文档流里的顶部横幅。宽度对齐 PageShell（`max-w-[1240px] mx-auto lg:px-10`），
  // 这样横幅和页面内容左右边缘齐平，不会显得外挂。
  //
  // `pt-[calc(...)]` 里的 `--tab-bar-h` 是 `MergedPageTabs` 写出来的吸顶标签栏厚度。
  // 必须让开它：这个组件是 `fixed top-16`，而横幅在文档流里 —— 两者不在同一个定位
  // 体系，横幅会从吸顶栏**下面**穿过去被盖住（实测标签栏 100% 被压）。
  // 变量没设（页面没有合并标签栏）时回退 0px，横幅贴顶不留空。
  if (expanded) {
    return (
      <div
        data-testid="pet-guide"
        className="mx-auto w-full max-w-[1240px] px-6 lg:px-10 pt-[calc(0.75rem+var(--tab-bar-h,0px))]"
      >
        <div
          data-testid="pet-guide-body"
          className="flex items-center gap-4 rounded-2xl border border-primary/30 bg-primary/[0.07] p-4"
        >
          {avatar}
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-foreground">{guide.title}</p>
            <p className="mt-1 min-h-[1.5em] text-sm leading-relaxed text-card-foreground">
              {displayText}
            </p>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-2">
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
              className="text-[11px] text-muted-foreground transition hover:text-foreground"
            >
              跳过
            </button>
          </div>
        </div>
      </div>
    )
  }

  // 收起态：右下角 64px 头像。固定在角落、完全脱离内容区，实测无遮挡。
  return (
    <div data-testid="pet-guide" className="fixed bottom-4 right-4 z-30 opacity-90 lg:right-8">
      {avatar}
    </div>
  )
}
