import { AudioLines, FlaskConical, Library, Mic2, Radio, Radar, Speech } from "lucide-react"
import { Link, useLocation } from "react-router-dom"

const items = [
  { path: "/workshop", label: "音色工坊", icon: Mic2 },
  { path: "/discover", label: "发掘音色", icon: Radar },
  { path: "/ft", label: "音色微调", icon: FlaskConical },
  { path: "/voices", label: "音色库", icon: Library },
  { path: "/tts", label: "语音合成", icon: Speech },
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/offlinevc", label: "离线工坊", icon: AudioLines },
]

// 注意：合并页（实时变声/语音合成/离线工坊）内部的 tab 自带就绪降级提示，
// 「离线变声」无模型时页面内会引导去训练，不再整页锁定。
export function StudioNav({ compact = false }: { compact?: boolean }) {
  const currentLocation = useLocation()

  return (
    <nav className={compact ? "flex flex-col gap-1" : "no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1"}>
      {items.map(({ path, label, icon: Icon }) => {
        const active = currentLocation.pathname.endsWith(path)
        return (
          <Link
            key={path}
            to={path}
            className={`inline-flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-xs transition ${active ? "bg-primary text-primary-foreground shadow-md" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}
          >
            <Icon className="h-4 w-4" />
            {label}
          </Link>
        )
      })}
    </nav>
  )
}
