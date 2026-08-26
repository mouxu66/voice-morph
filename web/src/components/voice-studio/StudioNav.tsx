import { Library, Mic2, Speech, Zap } from "lucide-react"
import { Link, useLocation } from "react-router-dom"

const items = [{ path: "/kangaroo", label: "袋鼠语音", icon: Zap }, { path: "/workshop", label: "音色工坊", icon: Mic2 }, { path: "/voices", label: "音色库", icon: Library }, { path: "/tts", label: "文字转语音", icon: Speech }]

export function StudioNav({ compact = false }: { compact?: boolean }) {
  const currentLocation = useLocation()
  return <nav className={compact ? "flex flex-col gap-1" : "flex flex-wrap gap-1 border-b border-border bg-card/70 px-5 py-3 shadow-md backdrop-blur-md sm:px-8 lg:px-12"}>{items.map(({ path, label, icon: Icon }) => { const active = currentLocation.pathname.endsWith(path); return <Link key={path} to={path} className={`inline-flex items-center gap-2 rounded-md px-3 py-2.5 text-xs transition ${active ? "bg-primary text-primary-foreground shadow-md" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}><Icon className="h-4 w-4" />{label}</Link> })}</nav>
}
