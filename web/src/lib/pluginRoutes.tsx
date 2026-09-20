/**
 * 插件清单 → 前端路由 / 侧栏导航的**桥接层**（插件化第 4 步）。
 *
 * 为什么需要它
 * ------------
 * 第 4 步之前，`App.tsx` 里 18 条 `<Route>` 与 `StudioNav.tsx` 里 8 个导航项都是**硬编码**，
 * 而「哪些能力存在」后端已经在 `m2_server/plugins/<id>/plugin.json` 里声明了（第 2 步）。
 * 两份清单各写各的，必然漂：后端加了能力前端不显示、前端留了项后端已经删了。
 *
 * 这一层就是那座桥：**只读 `GET /api/plugins`，不再手写任何路径 / 标签 / 图标**。
 *
 * 三个必须记住的点（都踩过或差点踩）
 * ----------------------------------
 * 1. **页面是具名导出**（`pages/Home/index.tsx` → `HomeRoute`），没有 default export。
 *    所以 `React.lazy` 不能写成 `() => import(...)`，必须显式取清单里指名的那一个。
 *    名字打错 = 白屏，而且**构建期不报错** —— 靠 `pluginRoutes.test.ts` 的跨语言门禁守。
 *
 * 2. **`import.meta.glob` 会多匹配 6 个**：`src/pages/` 下有 14 个 `index.tsx`，但只有 8 个
 *    是真路由，其余 6 个（`Audiobook` / `Cascade` / `Discover` / `Effects` / `Ft` /
 *    `VoiceMarket`）是被别的页当 **tab** 消费的。所以**必须**拿清单的 `routes[].module`
 *    当白名单筛，不能把 glob 的结果直接铺成路由。
 *
 * 3. **图标在清单里是字符串**（`"Home"`），要经下面那张**静态**注册表映射回组件。
 *    不能用 `import * as icons from "lucide-react"` —— 那会把一千多个图标全打进包里。
 */
import { lazy, useCallback, useEffect, useState, type ComponentType, type LazyExoticComponent } from "react"
import { AudioLines, Home, Library, Mic2, PawPrint, Radio, Speech, Wrench, type LucideIcon } from "lucide-react"
import { getPlugins } from "@/api/client"
import type { PluginCatalog, PluginEntry } from "@/types"

/**
 * 页面模块的 glob 映射，key 形如 `../pages/Home/index.tsx`。
 *
 * 相对本文件（`src/lib/`）解析。**导出是为了给门禁用** —— 门禁必须用同一个 key 公式
 * 去校验清单里的每个 `module`，在测试里重写一遍公式就等于不校验公式本身。
 */
export const pageModules = import.meta.glob("../pages/*/index.tsx")

/** 清单里的 `module` → glob key。两处必须一致，所以只留这一个函数。 */
export function pageKey(module: string): string {
  return `../pages/${module}/index.tsx`
}

/**
 * 清单里的 icon 字符串 → 组件。**静态**表（tree-shaking 友好）。
 * 门禁 `pluginRoutes.test.ts` 断言清单用到的每个名字都在这张表里。
 */
export const knownIcons: Record<string, LucideIcon> = {
  AudioLines,
  Home,
  Library,
  Mic2,
  PawPrint,
  Radio,
  Speech,
  Wrench,
}

/**
 * 一个插件的能力是否在界面上出现。
 *
 * 看 **`enabled`**（第 6 步的开关结果），**不是** `state !== "disabled"`：
 * 被别的启用能力依赖时，用户关了它但后端仍保留（否则依赖方会变砖），
 * 此时 `state='disabled'` 而 `enabled=true` —— 拿 `state` 判会把一个**后端仍挂载着**
 * 的能力从界面上抹掉，用户就再也找不到入口。
 *
 * 但 **`core` 恒出现**：清单里 `core` 的定义就是「不可关闭」，而 `core.system` 恰好
 * 持有 `/home`。万一 `outputs/plugins.json` 被手工改坏，这条兜底保证首页还在，
 * 界面不会退化成「一条路由都没有的空壳」。
 *
 * `enabled` 缺失时按"显示"处理：旧版后端还没这个字段，不该让整个界面变空。
 */
function isVisible(plugin: PluginEntry): boolean {
  return Boolean(plugin.core) || plugin.enabled !== false
}

/**
 * 页内 UI（tab / 面板 / 按钮）按能力清单显隐时的查询：插件被关 → false。
 *
 * 与 `isVisible` 同一策略：core 恒可见；`enabled` 缺字段（旧后端）按可见处理。
 * 清单没拿到 / 没这个 id 也按可见 —— 这里只做「关掉就藏」，不替旧后端下结论。
 */
export function pluginVisible(catalog: PluginCatalog | null | undefined, id: string): boolean {
  if (!catalog) return true
  const p = catalog.plugins.find((x) => x.id === id)
  return p ? isVisible(p) : true
}

/** 页面缺失 / 导出名不对时的占位。不抛异常 —— 一条坏声明不该让整个界面白屏。 */
function missingPage(module: string, exportName?: string): ComponentType {
  return function MissingPage() {
    return (
      <div className="mx-auto w-full max-w-[1240px] px-6 pb-20 pt-8 lg:px-10 lg:pt-10">
        <div className="rounded-2xl border border-dashed border-border bg-background/50 p-6">
          <h1 className="text-xl font-semibold tracking-tight text-foreground">页面没找到</h1>
          <p className="mt-1.5 max-w-3xl text-sm leading-6 text-muted-foreground">
            能力清单里声明了这个页面，但前端没有对应的实现。多半是页面被删了、目录改名了，
            或者清单里的导出名写错了。
          </p>
          <p className="mt-3 font-mono text-xs text-muted-foreground">
            pages/{module}/index.tsx{exportName ? ` · 期望导出 ${exportName}` : "（glob 里没有这个 key）"}
          </p>
        </div>
      </div>
    )
  }
}

/**
 * `module::export` → 懒加载组件。
 *
 * **必须缓存**：`lazy()` 每次调用都返回一个**新的**组件类型，若在渲染期重新创建，
 * React 会把整页卸载重建（表现为每次状态更新都闪一下、输入框失焦）。
 */
const pageCache = new Map<string, LazyExoticComponent<ComponentType>>()

/** 清单声明的一条路由 → 可渲染的懒加载组件。 */
export function resolvePage(module: string, exportName: string): LazyExoticComponent<ComponentType> {
  const cacheKey = `${module}::${exportName}`
  const hit = pageCache.get(cacheKey)
  if (hit) return hit

  const load = pageModules[pageKey(module)]
  const comp = load
    ? lazy(async () => {
        const mod = (await load()) as Record<string, unknown>
        const Page = mod[exportName]
        // 导出名不对也走占位而不是抛：抛出去会炸掉整棵树（见下面 RouteErrorBoundary 的注释）
        return { default: typeof Page === "function" ? (Page as ComponentType) : missingPage(module, exportName) }
      })
    : lazy(async () => ({ default: missingPage(module) }))

  pageCache.set(cacheKey, comp)
  return comp
}

/** 清页面缓存 —— **只给测试用**。 */
export function resetPageCache(): void {
  pageCache.clear()
}

export interface RouteSpec {
  path: string
  pluginId: string
  module: string
  export: string
  Component: LazyExoticComponent<ComponentType>
}

export interface LegacySpec {
  path: string
  redirect: string
  pluginId: string
}

/** 清单 → 真页面路由 + 旧路由重定向。顺序沿用清单的插件顺序（`order`）。 */
export function buildRoutes(catalog: PluginCatalog): { routes: RouteSpec[]; legacy: LegacySpec[] } {
  const visible = catalog.plugins.filter(isVisible)
  const routes = visible.flatMap((p) =>
    p.routes.map((r) => ({
      path: r.path,
      pluginId: p.id,
      module: r.module,
      export: r.export,
      Component: resolvePage(r.module, r.export),
    })),
  )
  const legacy = visible.flatMap((p) =>
    p.legacyRoutes.map((r) => ({ path: r.path, redirect: r.redirect, pluginId: p.id })),
  )
  return { routes, legacy }
}

export interface NavItemSpec {
  path: string
  label: string
  icon: LucideIcon | null
  /** 首页是根路径，必须精确匹配（其余沿用 endsWith，见 StudioNav 的 isActive） */
  exact: boolean
}

/** 清单 → 某一组的导航项，按清单里的 `nav.order` 升序。 */
export function navItems(catalog: PluginCatalog, group: "start" | "more"): NavItemSpec[] {
  const decls: { path: string; label: string; icon?: string; order: number }[] = []
  for (const p of catalog.plugins) {
    if (!isVisible(p)) continue
    for (const r of p.routes) {
      if (r.nav && r.nav.group === group) {
        decls.push({ path: r.path, label: r.nav.label, icon: r.nav.icon, order: r.nav.order })
      }
    }
  }
  return decls
    .sort((a, b) => a.order - b.order)
    .map((d) => ({
      path: d.path,
      label: d.label,
      icon: d.icon ? (knownIcons[d.icon] ?? null) : null,
      exact: d.path === "/home",
    }))
}

// ---------------------------------------------------------------- catalog 单例

export type CatalogState =
  | { status: "loading" }
  | { status: "ready"; catalog: PluginCatalog }
  | { status: "error"; message: string }

let cached: PluginCatalog | null = null
let inflight: Promise<PluginCatalog> | null = null

function loadCatalog(): Promise<PluginCatalog> {
  if (cached) return Promise.resolve(cached)
  // 合并并发请求：`App` 与 `StudioNav` 都会调这个 hook，不该发两次
  inflight ??= getPlugins()
    .then((c) => {
      cached = c
      return c
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

/** 清缓存 —— **只给测试用**（真实进程里清单在一个会话内不变）。 */
export function resetCatalogCache(): void {
  cached = null
  inflight = null
}

/**
 * 读一次能力清单（模块级缓存，全会话只发一次请求）。
 *
 * 失败时返回 `status: "error"` 而**不是**静默降级成空清单 —— 空清单会让界面变成
 * 「一个导航项都没有、所有路由都不存在」的样子，用户只会看到白屏而不知道后端出了问题。
 * 所以由调用方把这个状态显式渲染出来，并给一个重试入口。
 */
export function usePluginCatalog(): { state: CatalogState; reload: () => void } {
  const [state, setState] = useState<CatalogState>(
    cached ? { status: "ready", catalog: cached } : { status: "loading" },
  )
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    if (cached) {
      setState({ status: "ready", catalog: cached })
      return
    }
    let alive = true
    setState({ status: "loading" })
    loadCatalog()
      .then((c) => {
        if (alive) setState({ status: "ready", catalog: c })
      })
      .catch((e: unknown) => {
        if (!alive) return
        setState({ status: "error", message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      alive = false
    }
  }, [nonce])

  const reload = useCallback(() => {
    resetCatalogCache()
    setNonce((n) => n + 1)
  }, [])

  return { state, reload }
}
