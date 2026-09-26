import { useMemo, useState } from "react"
import { ChevronDown } from "lucide-react"
import { Link, useLocation } from "react-router-dom"
import { cn } from "@/lib/utils"
import { NAV_GROUPS, navItems, usePluginCatalog, type NavItemSpec } from "@/lib/pluginRoutes"

/**
 * 主导航（插件化第 4 步：改由能力清单驱动）。
 *
 * 「有哪些页面、叫什么名字、用什么图标、归哪一组、组内排第几」全部来自后端
 * `m2_server/plugins/<id>/plugin.json` 的 `routes[].nav`；本文件只负责把它表达清楚：
 * 按组呈现（组的顺序与中文标签来自 `pluginRoutes.NAV_GROUPS`）+ 当前项指示。
 *
 * 两个历史决定也搬进了清单，别再回来找数组：
 *   · 「试音间」归音色组（09-18 拍板它不是新引擎而是既有能力的统一入口；
 *     09-26 四组化后它本质是"挑音色听效果"的地方）
 *     → `plugins/sound.audition/plugin.json` 的 `nav.order = 20`
 *   · 极简模式 = 主路径（开始 + 变声）常显，其余组折叠；当前页所在组自动展开
 *     —— 停在一组里却看不见它的任何项，就是"找不到当前页"
 *
 * ⚠️ 图标在清单里是**字符串**，经 `pluginRoutes.knownIcons` 那张静态表映射回组件。
 * 名字写错时这里拿到 `null`（界面留白位而不是崩），但 CI 有门禁会红 ——
 * 见 `web/src/lib/pluginRoutes.test.ts`。
 */

/** 极简模式下**常显**的组（用户的主路径）；其余组折叠、点标题展开。 */
const SIMPLE_ALWAYS_OPEN = new Set(["start", "vc"])

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

  // 四组一次算齐；空组不渲染 —— 组里所有能力被关掉时，光秃秃一个组标题比没有更奇怪
  const groups = useMemo(
    () =>
      state.status === "ready"
        ? NAV_GROUPS.map((g) => ({ ...g, items: navItems(state.catalog, g.id) })).filter(
            (g) => g.items.length > 0,
          )
        : [],
    [state],
  )

  // 首页是根路径必须精确匹配；其余沿用 endsWith
  const isActive = (item: NavItemSpec) =>
    item.exact ? pathname === item.path : pathname.endsWith(item.path)

  // 当前页所在的组：折叠组停靠时自动展开。不能在 useState 初始化器里一次性定死 ——
  // 清单是异步来的，首帧 groups 还是空的，初始化器只会算出 undefined。
  const activeGroup = groups.find((g) => g.items.some(isActive))?.id

  // 手动开合记录。`undefined` = 没动过，跟随 activeGroup 自动决定；
  // 用户点过一次后就以手动为准，不再自动改（否则展开态会跟着跳页乱跳）。
  const [manual, setManual] = useState<Record<string, boolean>>({})

  // 清单还没到 / 读不到：侧栏**不能就这么空着**。空侧栏和"这个应用没有导航"长得一模一样，
  // 用户会以为坏了却不知道坏在哪。加载中给骨架（顺带稳住布局，避免首帧跳一下），
  // 出错给一行说明（真正的重试入口在主区域那张 NoticePanel 上）。
  if (state.status === "loading") return <NavSkeleton variant={variant} />
  if (state.status === "error") return <NavUnavailable variant={variant} />

  if (variant === "bar") {
    // 窄屏横条是滚动条不是目录，没有折叠的空间概念 —— 按组序平铺全部项
    return (
      <nav className="no-scrollbar flex gap-1.5 overflow-x-auto px-4 py-1.5" aria-label="主导航">
        {groups
          .flatMap((g) => g.items)
          .map((item) => (
            <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
          ))}
      </nav>
    )
  }

  return (
    <nav className="flex flex-col gap-6" aria-label="主导航">
      {groups.map((g) => {
        const collapsible = simpleMode && !SIMPLE_ALWAYS_OPEN.has(g.id)
        const open = collapsible ? (manual[g.id] ?? g.id === activeGroup) : true
        return (
          <NavGroup
            key={g.id}
            label={g.label}
            collapsible={collapsible}
            open={open}
            onToggle={() => setManual((m) => ({ ...m, [g.id]: !open }))}
          >
            {open &&
              g.items.map((item) => (
                <NavLink key={item.path} item={item} active={isActive(item)} onNavigate={onNavigate} />
              ))}
          </NavGroup>
        )
      })}
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
        // 坏掉的能力照样能点进去（页面还在，只是后端没挂路由）——
        // 点进去看到报错，比根本找不到入口更容易定位问题
        title={
          item.broken
            ? "这个能力装了但没起来（缺依赖或加载失败）—— 到「能力管理」看具体原因"
            : undefined
        }
        className={cn(
          "group relative flex items-center gap-2.5 whitespace-nowrap rounded-lg px-3 py-2 text-[13px] transition",
          active
            ? "bg-primary/[0.12] font-medium text-foreground"
            : item.broken
              ? // 坏掉的项**留在原位**、只是哑掉：用户不知道它坏了，最需要看见。
                // （主动关掉的走的是另一条路 —— 直接从导航移出，见 pluginRoutes.isVisible）
                "text-muted-foreground/70 hover:bg-muted/60 hover:text-foreground"
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
          <Icon
            className={cn(
              "h-4 w-4 shrink-0",
              item.broken
                ? "text-red-600 dark:text-red-400"
                : active
                  ? "text-primary"
                  : "text-muted-foreground/80",
            )}
          />
        ) : (
          // 清单里的图标名不在 knownIcons 里（门禁会拦住，这里只保证界面不歪）
          <span className="h-4 w-4 shrink-0" aria-hidden="true" />
        )}
        {item.label}
        {item.broken ? (
          <>
            <span className="ml-auto h-1.5 w-1.5 shrink-0 rounded-full bg-red-500" aria-hidden="true" />
            <span className="sr-only">（未加载）</span>
          </>
        ) : null}
      </Link>
    </li>
  )
}
