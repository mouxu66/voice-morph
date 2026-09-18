import { useState } from "react"
import { ChevronDown, Home, Library, Mic2, PawPrint, Radio, Shirt, Speech, Wrench } from "lucide-react"
import { Link, useLocation } from "react-router-dom"
import { cn } from "@/lib/utils"

type NavItem = {
  path: string
  label: string
  icon: typeof Home
  exact?: boolean
}

/**
 * 产品既定信息架构（09-15 平民化改造，勿擅自改动）：
 * 极简模式 = 首页 + 三条主路径（输字变声 / 训练变声 / 工具箱），其余收进「更多功能」。
 * 这里只负责把它表达清楚——分组呈现 + 当前项指示，不改动分组归属。
 *
 * 2026-09-18 例外：「试衣间」加进「开始」组。它不是一个新引擎，而是既有能力的统一入口
 * （挑音色 → 试穿 → 满意了再去精调），所以放在最上层而不是塞进「更多功能」。
 */
const START_ITEMS: NavItem[] = [
  { path: "/home", label: "首页", icon: Home, exact: true },
  { path: "/fitting", label: "试衣间", icon: Shirt },
  { path: "/tts", label: "输字变声", icon: Speech },
  { path: "/workshop", label: "训练变声", icon: Mic2 },
  { path: "/offlinevc", label: "工具箱", icon: Wrench },
]

const MORE_ITEMS: NavItem[] = [
  { path: "/voices", label: "我的音色", icon: Library },
  { path: "/live", label: "实时变声", icon: Radio },
  { path: "/pet-market", label: "桌宠皮肤", icon: PawPrint },
]

export function StudioNav({
  variant = "bar",
  simpleMode = false,
  onNavigate,
}: {
  /** sidebar = 桌面侧栏 / 抽屉内；bar = 窄屏横向滚动条 */
  variant?: "sidebar" | "bar"
  simpleMode?: boolean
  onNavigate?: () => void
}) {
  const { pathname } = useLocation()
  // 极简模式下停在「更多功能」里的页面时直接展开，避免"找不到当前页"
  const [moreOpen, setMoreOpen] = useState(
    simpleMode ? MORE_ITEMS.some((it) => pathname.endsWith(it.path)) : true,
  )

  // 首页是根路径必须精确匹配；其余沿用 endsWith
  const isActive = (item: NavItem) => (item.exact ? pathname === item.path : pathname.endsWith(item.path))

  if (variant === "bar") {
    return (
      <nav className="no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1.5" aria-label="主导航">
        {[...START_ITEMS, ...MORE_ITEMS].map((item) => (
          <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
        ))}
      </nav>
    )
  }

  return (
    <nav className="flex flex-col gap-6" aria-label="主导航">
      <NavGroup label="开始">
        {START_ITEMS.map((item) => (
          <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
        ))}
      </NavGroup>

      <NavGroup
        label="更多功能"
        collapsible={simpleMode}
        open={moreOpen}
        onToggle={() => setMoreOpen((v) => !v)}
      >
        {(simpleMode ? moreOpen : true) &&
          MORE_ITEMS.map((item) => (
            <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
          ))}
      </NavGroup>
    </nav>
  )
}

function NavGroup({
  label,
  collapsible = false,
  open = true,
  onToggle,
  children,
}: {
  label: string
  collapsible?: boolean
  open?: boolean
  onToggle?: () => void
  children: React.ReactNode
}) {
  return (
    <div>
      {collapsible ? (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="mb-1.5 flex w-full items-center gap-1 px-3 text-[11px] font-medium tracking-wide text-muted-foreground transition hover:text-foreground"
        >
          {label}
          <ChevronDown className={cn("h-3 w-3 transition-transform", !open && "-rotate-90")} />
        </button>
      ) : (
        <p className="mb-1.5 px-3 text-[11px] font-medium tracking-wide text-muted-foreground">{label}</p>
      )}
      <ul className="flex flex-col gap-0.5">{children}</ul>
    </div>
  )
}

function NavLink({
  item,
  active,
  onNavigate,
}: {
  item: NavItem
  active: boolean
  onNavigate?: () => void
}) {
  const Icon = item.icon
  return (
    // shrink-0 不能省：横向导航条里 flex 会把标签压成一列竖排汉字
    <li className="shrink-0">
      <Link
        to={item.path}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        className={cn(
          "group relative flex items-center gap-2.5 whitespace-nowrap rounded-lg px-3 py-2 text-[13px] transition",
          active
            ? "bg-primary/[0.12] font-medium text-foreground"
            : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
        )}
      >
        {/* 当前页指示条：比整块高亮更轻，扫视时更容易定位 */}
        <span
          className={cn(
            "absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-full bg-primary transition-opacity",
            active ? "opacity-100" : "opacity-0",
          )}
          aria-hidden="true"
        />
        <Icon className={cn("h-4 w-4 shrink-0", active ? "text-primary" : "text-muted-foreground/80")} />
        {item.label}
      </Link>
    </li>
  )
}
