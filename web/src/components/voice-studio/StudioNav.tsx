import { useMemo, useState } from "react"
import { ChevronDown } from "lucide-react"
import { Link, useLocation } from "react-router-dom"
import { cn } from "@/lib/utils"
import { navItems, usePluginCatalog, type NavItemSpec } from "@/lib/pluginRoutes"

/**
 * 主导航（插件化第 4 步：改由能力清单驱动）。
 *
 * 以前这里有两张硬编码数组 `START_ITEMS` / `MORE_ITEMS`，现在**没有**了 ——
 * 「有哪些页面、叫什么名字、用什么图标、归哪一组、组内排第几」全部来自后端
 * `m2_server/plugins/<id>/plugin.json` 的 `routes[].nav`，本文件只负责把它表达清楚：
 * 分组呈现 + 当前项指示。
 *
 * 于是**改信息架构要改的地方只有一处** —— `plugin.json`。两个历史决定也随之搬了过去，
 * 别再回来找数组：
 *   · 「试音间」在「开始」组（09-18 拍板，它不是新引擎而是既有能力的统一入口）
 *     → `plugins/sound.audition/plugin.json` 的 `nav.order = 20`
 *   · 极简模式 = 首页 + 主路径，其余收进「更多功能」
 *     → 分组由 `nav.group`（`start` / `more`）表达
 *
 * ⚠️ 图标在清单里是**字符串**，经 `pluginRoutes.knownIcons` 那张静态表映射回组件。
 * 名字写错时这里拿到 `null`（界面留白位而不是崩），但 CI 有门禁会红 ——
 * 见 `web/src/lib/pluginRoutes.test.ts`。
 */
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
  const { state } = usePluginCatalog()

  const startItems = useMemo(
    () => (state.status === "ready" ? navItems(state.catalog, "start") : []),
    [state],
  )
  const moreItems = useMemo(
    () => (state.status === "ready" ? navItems(state.catalog, "more") : []),
    [state],
  )

  // 首页是根路径必须精确匹配；其余沿用 endsWith
  const isActive = (item: NavItemSpec) =>
    item.exact ? pathname === item.path : pathname.endsWith(item.path)

  // 极简模式下停在「更多功能」里的页面时直接展开，避免"找不到当前页"。
  // `null` = 还没被手动干预，跟随当前页自动决定；用户点过折叠按钮后就不再自动改。
  // （不能像以前那样用 useState 初始化器一次性定死 —— 清单是异步来的，
  //  首帧 items 还是空的，初始化器只会算出 false。）
  const [moreOpen, setMoreOpen] = useState<boolean | null>(null)
  const moreExpanded = moreOpen ?? moreItems.some(isActive)

  // 清单还没到 / 读不到：侧栏**不能就这么空着**。空侧栏和"这个应用没有导航"长得一模一样，
  // 用户会以为坏了却不知道坏在哪。加载中给骨架（顺带稳住布局，避免首帧跳一下），
  // 出错给一行说明（真正的重试入口在主区域那张 NoticePanel 上）。
  if (state.status === "loading") return <NavSkeleton variant={variant} />
  if (state.status === "error") return <NavUnavailable variant={variant} />

  if (variant === "bar") {
    return (
      <nav className="no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1.5" aria-label="主导航">
        {[...startItems, ...moreItems].map((item) => (
          <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
        ))}
      </nav>
    )
  }

  return (
    <nav className="flex flex-col gap-6" aria-label="主导航">
      <NavGroup label="开始">
        {startItems.map((item) => (
          <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
        ))}
      </NavGroup>

      <NavGroup
        label="更多功能"
        collapsible={simpleMode}
        open={moreExpanded}
        onToggle={() => setMoreOpen(!moreExpanded)}
      >
        {(simpleMode ? moreExpanded : true) &&
          moreItems.map((item) => (
            <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
          ))}
      </NavGroup>
    </nav>
  )
}

/** 清单加载中的占位。与真实导航同高，避免首帧布局跳一下。 */
function NavSkeleton({ variant }: { variant: "sidebar" | "bar" }) {
  const rows = [0, 1, 2]
  if (variant === "bar") {
    return (
      <nav className="flex gap-1.5 overflow-hidden px-4 py-1.5" aria-label="主导航" aria-busy="true">
        <span className="sr-only">正在读取能力清单</span>
        {rows.map((i) => (
          <div key={i} className="h-8 w-24 shrink-0 animate-pulse rounded-lg bg-muted/50" />
        ))}
      </nav>
    )
  }
  return (
    <nav className="flex flex-col gap-0.5 px-3" aria-label="主导航" aria-busy="true">
      <span className="sr-only">正在读取能力清单</span>
      {rows.map((i) => (
        <div key={i} className="h-9 animate-pulse rounded-lg bg-muted/50" />
      ))}
    </nav>
  )
}

/** 清单读不到时的降级：说明一句，不假装导航还在。重试在主区域的 NoticePanel 上。 */
function NavUnavailable({ variant }: { variant: "sidebar" | "bar" }) {
  return (
    <p
      className={cn(
        "text-[12px] leading-5 text-muted-foreground",
        variant === "bar" ? "px-4 py-2" : "px-3 py-2",
      )}
    >
      读不到能力清单，导航暂不可用
    </p>
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
  item: NavItemSpec
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
        {Icon ? (
          <Icon className={cn("h-4 w-4 shrink-0", active ? "text-primary" : "text-muted-foreground/80")} />
        ) : (
          // 清单里的图标名不在 knownIcons 里（门禁会拦住，这里只保证界面不歪）
          <span className="h-4 w-4 shrink-0" aria-hidden="true" />
        )}
        {item.label}
      </Link>
    </li>
  )
}
