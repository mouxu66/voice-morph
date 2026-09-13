import { useState } from "react"
import { AudioLines, ChevronDown, ChevronUp, Library, Mic2, PawPrint, Radio, Speech } from "lucide-react"
import { Link, useLocation } from "react-router-dom"

const items = [
  { path: "/workshop", label: "音色工坊", icon: Mic2 },
  { path: "/voices", label: "音色库", icon: Library },
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/tts", label: "语音合成", icon: Speech },
  { path: "/offlinevc", label: "离线工坊", icon: AudioLines },
  { path: "/pet-market", label: "人偶市场", icon: PawPrint },
]

/** 极简模式：侧边栏只留「变声」相关的核心入口，进阶功能收进「更多功能」 */
const CORE_ITEMS = [
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/voices", label: "音色库", icon: Library },
  { path: "/tts", label: "语音合成", icon: Speech },
]
const MORE_ITEMS = [
  { path: "/workshop", label: "音色工坊", icon: Mic2 },
  { path: "/offlinevc", label: "离线工坊", icon: AudioLines },
  { path: "/pet-market", label: "人偶市场", icon: PawPrint },
]

// 注意：合并页（音色工坊/音色库/实时变声/语音合成/离线工坊）内部的 tab 自带就绪降级提示，
// 「离线变声」无模型时页面内会引导去训练，不再整页锁定。
export function StudioNav({ compact = false, simpleMode = false }: { compact?: boolean; simpleMode?: boolean }) {
  const currentLocation = useLocation()
  // 极简模式下，若当前正停留在「更多功能」里的页面，进入时直接展开并高亮，避免"找不到当前页"
  const [expanded, setExpanded] = useState(
    simpleMode ? MORE_ITEMS.some((it) => currentLocation.pathname.endsWith(it.path)) : false,
  )

  const linkClass = (active: boolean) =>
    `inline-flex shrink-0 items-center gap-2 rounded-md px-3 py-2 text-xs transition ${active ? "bg-primary text-primary-foreground shadow-md" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`

  return (
    <nav className={compact ? "flex flex-col gap-1" : "no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1"}>
      {(simpleMode ? CORE_ITEMS : items).map(({ path, label, icon: Icon }) => {
        const active = currentLocation.pathname.endsWith(path)
        return (
          <Link key={path} to={path} className={linkClass(active)}>
            <Icon className="h-4 w-4" />
            {label}
          </Link>
        )
      })}
      {simpleMode && (
        <>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className={`${linkClass(false)} justify-between`}
          >
            {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            更多功能
          </button>
          {expanded &&
            MORE_ITEMS.map(({ path, label, icon: Icon }) => {
              const active = currentLocation.pathname.endsWith(path)
              return (
                <Link key={path} to={path} className={linkClass(active)}>
                  <Icon className="h-4 w-4" />
                  {label}
                </Link>
              )
            })}
        </>
      )}
    </nav>
  )
}