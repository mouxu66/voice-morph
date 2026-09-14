import { useState } from "react"
import { ChevronDown, ChevronUp, Home, Library, Mic2, PawPrint, Radio, Speech, Wrench } from "lucide-react"
import { Link, useLocation } from "react-router-dom"

const items = [
  { path: "/home", label: "首页", icon: Home, exact: true },
  { path: "/workshop", label: "训练变声", icon: Mic2 },
  { path: "/voices", label: "我的音色", icon: Library },
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/tts", label: "输字变声", icon: Speech },
  { path: "/offlinevc", label: "工具箱", icon: Wrench },
  { path: "/pet-market", label: "桌宠皮肤", icon: PawPrint },
]

/**
 * 极简模式：导航收敛成「首页 + 三条主路径」——
 * 免训练变声（秒出效果）/ 训练变声（攒素材练嗓子）/ 工具箱（整段变声等进阶），
 * 其余页面收进「更多功能」。
 */
const CORE_ITEMS = [
  { path: "/home", label: "首页", icon: Home, exact: true },
  { path: "/tts", label: "免训练变声", icon: Speech },
  { path: "/workshop", label: "训练变声", icon: Mic2 },
  { path: "/offlinevc", label: "工具箱", icon: Wrench },
]
const MORE_ITEMS = [
  { path: "/voices", label: "我的音色", icon: Library },
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/pet-market", label: "桌宠皮肤", icon: PawPrint },
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

  // 激活判断：首页是根路径，必须精确匹配（否则连 /home/something 也会点亮）；其余沿用 endsWith
  const isActive = (item: { path: string; exact?: boolean }) =>
    item.exact ? currentLocation.pathname === item.path : currentLocation.pathname.endsWith(item.path)

  return (
    <nav className={compact ? "flex flex-col gap-1" : "no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1"}>
      {(simpleMode ? CORE_ITEMS : items).map((item) => {
        const active = isActive(item)
        return (
          <Link key={item.path} to={item.path} className={linkClass(active)}>
            <item.icon className="h-4 w-4" />
            {item.label}
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
            MORE_ITEMS.map((item) => {
              const active = isActive(item)
              return (
                <Link key={item.path} to={item.path} className={linkClass(active)}>
                  <item.icon className="h-4 w-4" />
                  {item.label}
                </Link>
              )
            })}
        </>
      )}
    </nav>
  )
}