import { useEffect, useState } from "react"
import { AudioLines, BookOpen, FlaskConical, Library, Lock, Mic2, Radio, Speech } from "lucide-react"
import { Link, useLocation } from "react-router-dom"
import { listRvcVoices } from "@/api/client"

const items = [
  { path: "/workshop", label: "音色工坊", icon: Mic2 },
  { path: "/ft", label: "音色微调", icon: FlaskConical },
  { path: "/voices", label: "音色库", icon: Library },
  { path: "/tts", label: "文字转语音", icon: Speech },
  { path: "/audiobook", label: "有声书", icon: BookOpen },
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/offlinevc", label: "离线变声", icon: AudioLines },
]

// 前置依赖：必须先训练出至少一个可用音色（model_ready）才能使用。
// 注意「实时变声」页本身包含训练流程，不能锁，否则会造成死锁。
const NEEDS_MODEL = new Set(["/offlinevc"])

export function StudioNav({ compact = false }: { compact?: boolean }) {
  const currentLocation = useLocation()
  // null = 未知（服务未就绪），此时不锁定
  const [hasReadyVoice, setHasReadyVoice] = useState<boolean | null>(null)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const r = await listRvcVoices()
        if (alive) setHasReadyVoice(r.voices.some((v) => v.model_ready))
      } catch {
        // 服务未就绪时保持现状，不锁定
      }
    }
    void tick()
    const timer = window.setInterval(tick, 10_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  return (
    <nav className={compact ? "flex flex-col gap-1" : "no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1"}>
      {items.map(({ path, label, icon: Icon }) => {
        const active = currentLocation.pathname.endsWith(path)
        const locked = NEEDS_MODEL.has(path) && hasReadyVoice === false
        const title = locked ? "需先完成至少一个音色的训练（可在「音色微调」或「实时变声」页完成）" : undefined
        const inner = (
          <>
            <Icon className="h-4 w-4" />
            {label}
            {locked && <Lock className="h-3 w-3 text-amber-500" />}
          </>
        )
        const cls = locked
          ? `inline-flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-xs opacity-55 ${active ? "bg-primary text-primary-foreground" : "text-muted-foreground"}`
          : `inline-flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-xs transition ${active ? "bg-primary text-primary-foreground shadow-md" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`
        if (locked) {
          return (
            <span key={path} title={title} aria-disabled className={`${cls} cursor-not-allowed`}>
              {inner}
            </span>
          )
        }
        return (
          <Link key={path} to={path} title={title} className={cls}>
            {inner}
          </Link>
        )
      })}
    </nav>
  )
}
