/**
 * 清单 ↔ 前端 的**跨语言契约**门禁。
 *
 * 为什么 Python 那边有一份还要在这里再写一份
 * ------------------------------------------
 * `m2_server/tests/test_plugin_manifest.py` 只能看到**源码文本**（正则扫 `.tsx`）。
 * 而第 4 步之后前端是**运行时**从清单里取 `module` / `export` / `icon` 的，
 * 中间隔着三样 Python 看不见的东西：
 *
 *   1. **`import.meta.glob` 的 key 公式** —— `../pages/${module}/index.tsx`。
 *      公式写错（少一层 `..`、`pages` 拼成 `page`）→ 每个页面都渲染成「页面没找到」，
 *      而**构建期一声不响**（glob 只是匹配不到，不是语法错误）。
 *   2. **图标注册表** —— 清单写 `"Home"`，前端表里没有就是没图标。不报错。
 *   3. **具名导出** —— 页面没有 default export，导出名对不上就是白屏。
 *
 * 所以这里**读真实的 `plugin.json`**（不手写 fixture —— 手写 fixture 只会照抄我自己的
 * 假设，见 `docs/犯错指南.md` 里 `[object Object]` 那次），逐个把上面三样撞一遍。
 */
import { existsSync, readFileSync, readdirSync } from "node:fs"
import { resolve } from "node:path"
import { describe, expect, it } from "vitest"
import type { PluginCatalog, PluginEntry, PluginRoute } from "@/types"
import {
  buildRoutes,
  closedNavCapabilities,
  knownIcons,
  navItems,
  pageKey,
  pageModules,
  pluginVisible,
  resetPageCache,
} from "./pluginRoutes"

/**
 * 找 `m2_server/plugins`。
 *
 * 不用 `fileURLToPath(import.meta.url)`：jsdom 环境下 `import.meta.url` 不是 `file:`
 * 协议（实测抛 `The URL must be of scheme file`）。改成从 cwd 逐级上溯，
 * 这样从 `web/` 或仓库根跑都行；**找不到就直接抛**，绝不静默返回一个空目录
 * —— 空目录会让下面所有断言「两边都空」地白绿。
 */
function findPluginsDir(): string {
  let dir = process.cwd()
  for (let i = 0; i < 4; i += 1) {
    const cand = resolve(dir, "m2_server", "plugins")
    if (existsSync(cand)) return cand
    dir = resolve(dir, "..")
  }
  throw new Error(`找不到 m2_server/plugins（cwd=${process.cwd()}）`)
}

const PLUGINS_DIR = findPluginsDir()

interface RawPlugin {
  id: string
  category: string
  order: number
  routes?: PluginRoute[]
  legacy_routes?: { path: string; redirect: string }[]
}

/** 直接读真实清单文件（不是 fixture）。 */
function readManifest(): RawPlugin[] {
  return readdirSync(PLUGINS_DIR, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => JSON.parse(readFileSync(resolve(PLUGINS_DIR, e.name, "plugin.json"), "utf-8")) as RawPlugin)
    .sort((a, b) => a.order - b.order)
}

/** 把真实清单包成 `PluginCatalog` 的形状，供纯函数测试用。 */
function asCatalog(overrides: Record<string, Partial<PluginEntry>> = {}): PluginCatalog {
  const plugins: PluginEntry[] = readManifest().map((raw) => ({
    id: raw.id,
    name: raw.id,
    kind: "builtin",
    category: raw.category as PluginEntry["category"],
    order: raw.order,
    summary: "",
    core: raw.category === "core",
    state: "ok",
    enabled: true,
    blockedBy: [],
    reasons: [],
    requires: [],
    routers: [],
    routes: raw.routes ?? [],
    legacyRoutes: raw.legacy_routes ?? [],
    extras: {},
    health: null,
    healthProbe: null,
    disableNote: "",
    ...overrides[raw.id],
  }))
  return {
    ok: true,
    counts: { total: plugins.length, ok: plugins.length, broken: 0, disabled: 0 },
    plugins,
    loaders: { routers: 0, loaded: 0, broken: [] },
    restartRequired: true,
    presets: [],
    preset: "full",
  }
}

const MANIFEST = readManifest()
const ALL_ROUTES = MANIFEST.flatMap((p) => (p.routes ?? []).map((r) => ({ plugin: p.id, route: r })))

describe("清单声明的页面都能被前端解析出来", () => {
  it("每个 routes[].module 都能在 glob 映射里找到 key", () => {
    const missing = ALL_ROUTES.filter(({ route }) => !pageModules[pageKey(route.module)]).map(
      ({ plugin, route }) => `${plugin}: ${route.path} → ${pageKey(route.module)}`,
    )
    expect(missing, `glob 里没有这些 key（公式或目录名对不上）：\n${missing.join("\n")}`).toEqual([])
  })

  it("每个 routes[].export 在页面模块里都是可调用的具名导出", async () => {
    const bad: string[] = []
    for (const { plugin, route } of ALL_ROUTES) {
      const mod = (await pageModules[pageKey(route.module)]!()) as Record<string, unknown>
      if (typeof mod[route.export] !== "function") {
        bad.push(`${plugin}: ${route.module}/index.tsx 里没有可调用的导出 ${route.export}`)
      }
    }
    expect(bad, `页面是具名导出，名字打错 = 白屏且构建期不报错：\n${bad.join("\n")}`).toEqual([])
  })

  it("清单用到的每个图标都在前端注册表里", () => {
    const used = new Set(
      MANIFEST.flatMap((p) => (p.routes ?? []).map((r) => r.nav?.icon).filter((x): x is string => Boolean(x))),
    )
    const unknown = [...used].filter((name) => !knownIcons[name])
    expect(unknown, `这些图标名不在 knownIcons 里（界面会缺图标且不报错）：${unknown.join("、")}`).toEqual([])
    // 反向：表里留了没人用的图标也只是浪费，但不该是空的
    expect(used.size).toBeGreaterThan(0)
  })
})

describe("glob 会多匹配，必须由清单当白名单", () => {
  it("glob 匹配到的模块数 > 清单声明的页面数（多出来的是被当 tab 消费的页面）", () => {
    const globbed = Object.keys(pageModules)
    const declared = new Set(ALL_ROUTES.map(({ route }) => pageKey(route.module)))
    // 这条断言是「必须过滤」这件事的证据：一旦有人把 glob 结果直接铺成路由，
    // 多出来的这些就会变成能直接访问、但没有导航入口的野页面。
    expect(globbed.length).toBeGreaterThan(declared.size)
    const extra = globbed.filter((k) => !declared.has(k))
    expect(extra.length, "多匹配数变了就要重新确认第 4 步的白名单仍然必要").toBeGreaterThan(0)
  })

  it("buildRoutes 只产出清单里的路由，不含多匹配的那些", () => {
    resetPageCache()
    const { routes } = buildRoutes(asCatalog())
    expect(routes.map((r) => r.path).sort()).toEqual([...new Set(ALL_ROUTES.map(({ route }) => route.path))].sort())
  })
})

describe("可见性与排序", () => {
  it("被关掉的插件不出现，但 core 恒在（否则首页可能整个消失）", () => {
    resetPageCache()
    const off = {
      "sound.tts": { state: "disabled" as const, enabled: false },
      "core.system": { state: "disabled" as const, enabled: false },
    }
    const { routes } = buildRoutes(asCatalog(off))
    const paths = routes.map((r) => r.path)
    expect(paths).not.toContain("/tts")
    expect(paths).toContain("/home")
    // 反例：如果 core 也照开关过滤，界面会一条路由都没有
    const { routes: onlyCore } = buildRoutes(
      asCatalog({ "core.system": { state: "disabled" as const, enabled: false } }),
    )
    expect(onlyCore.length).toBeGreaterThan(0)
  })

  it("★ 可见性看 enabled 而不是 state（被依赖而保留的能力必须还在界面上）", () => {
    resetPageCache()
    // 用户关了它，但 sound.audiobook 还依赖它 → 后端仍挂载（否则依赖方变砖）。
    // 此时把它从界面上抹掉 = 用户再也找不到一个实际可用的入口。
    const { routes } = buildRoutes(
      asCatalog({ "sound.tts": { state: "disabled" as const, enabled: true } }),
    )
    expect(routes.map((r) => r.path)).toContain("/tts")

    // 对照：真的关掉了就不该出现（否则上一条等于把开关废了）
    const { routes: off } = buildRoutes(
      asCatalog({ "sound.tts": { state: "disabled" as const, enabled: false } }),
    )
    expect(off.map((r) => r.path)).not.toContain("/tts")
  })

  it("旧版后端没有 enabled 字段时按可见处理（不该整个界面变空）", () => {
    resetPageCache()
    const catalog = asCatalog()
    for (const p of catalog.plugins) delete (p as Partial<PluginEntry>).enabled
    expect(buildRoutes(catalog).routes.length).toBeGreaterThan(0)
    expect(navItems(catalog, "start").length).toBeGreaterThan(0)
  })

  it("pluginVisible：被关的插件 → false，core / 缺字段 / 清单缺失 → 可见", () => {
    const catalog = asCatalog({
      "hook.wechat": { state: "disabled" as const, enabled: false },
      "sound.audiobook": { state: "disabled" as const, enabled: true }, // 被依赖而保留
    })
    expect(pluginVisible(catalog, "hook.wechat")).toBe(false)
    expect(pluginVisible(catalog, "sound.audiobook")).toBe(true) // 看 enabled 不看 state
    expect(pluginVisible(catalog, "core.system")).toBe(true)
    expect(pluginVisible(null, "hook.wechat")).toBe(true) // 清单没拿到：不隐藏
    expect(pluginVisible(catalog, "no.such-plugin")).toBe(true) // 清单里没有：不替后端下结论

    const legacy = asCatalog()
    for (const p of legacy.plugins) delete (p as Partial<PluginEntry>).enabled
    expect(pluginVisible(legacy, "hook.wechat")).toBe(true) // 旧后端缺 enabled：可见
  })

  it("导航按清单 nav.order 升序，分组不串", () => {
    resetPageCache()
    const catalog = asCatalog()
    const start = navItems(catalog, "start").map((n) => n.path)
    const more = navItems(catalog, "more").map((n) => n.path)
    // 期望值从真实清单算，但**顺序**必须与 nav.order 一致 —— 这里钉的是排序真的生效了
    const expected = (group: "start" | "more") =>
      ALL_ROUTES.filter(({ route }) => route.nav?.group === group)
        .sort((a, b) => (a.route.nav!.order ?? 0) - (b.route.nav!.order ?? 0))
        .map(({ route }) => route.path)
    expect(start).toEqual(expected("start"))
    expect(more).toEqual(expected("more"))
    expect(start.length).toBeGreaterThan(0)
    expect(more.length).toBeGreaterThan(0)
  })

  it("每条导航项都能拿到图标组件，且首页是精确匹配", () => {
    resetPageCache()
    const catalog = asCatalog()
    for (const group of ["start", "more"] as const) {
      for (const item of navItems(catalog, group)) {
        expect(item.icon, `${item.path} 没有解析出图标`).toBeTruthy()
        expect(item.label.length).toBeGreaterThan(0)
      }
    }
    expect(navItems(catalog, "start").find((n) => n.path === "/home")?.exact).toBe(true)
    expect(navItems(catalog, "more").every((n) => !n.exact)).toBe(true)
  })

  // ---- 「主动关掉」与「被动坏掉」必须分开处理 -------------------------------
  //
  // 这一刀是「某块功能凭空消失」的解药：
  // · 主动关掉 = 用户自己做的决定，他记得 → 从导航移出，底栏留回头路；
  // · 被动坏掉 = 用户根本不知道 → **留在导航里**但标 broken，让界面哑掉它。

  it("★ 主动关掉的从导航消失，被动坏掉的留在导航并被标记", () => {
    resetPageCache()
    // /tts 在「开始」组，主动关掉 → 移出导航
    const off = asCatalog({ "sound.tts": { state: "disabled" as const, enabled: false } })
    expect(navItems(off, "start").map((n) => n.path)).not.toContain("/tts")
    expect(navItems(off, "start").length).toBeGreaterThan(0) // 别把整条导航也清空了

    // 缺依赖（enabled 仍是 true，只是后端没挂上）→ 必须留在导航里
    const broken = asCatalog({ "sound.tts": { state: "broken" as const, enabled: true } })
    const item = navItems(broken, "start").find((n) => n.path === "/tts")
    expect(item, "坏掉的项被一起过滤掉了 —— 用户会只看到「功能没了」").toBeTruthy()
    expect(item?.broken).toBe(true)

    // 反面：正常的项不许被误标，否则整条导航全是红点，等于没有标记
    expect(navItems(asCatalog(), "start").every((n) => !n.broken)).toBe(true)
    expect(navItems(asCatalog(), "more").every((n) => !n.broken)).toBe(true)
  })

  it("★ closedNavCapabilities 只算「本来有导航项」的被关能力", () => {
    const catalog = asCatalog({
      "sound.tts": { state: "disabled" as const, enabled: false }, // 有 /tts 导航项 → 要计入
      "sound.audiobook": { state: "disabled" as const, enabled: false }, // 没有导航项 → 不计入
      "core.system": { state: "disabled" as const, enabled: false }, // core 恒可见 → 不计入
    })
    const ids = closedNavCapabilities(catalog).map((p) => p.id)
    expect(ids).toContain("sound.tts")
    // 关掉一个没有独立页面的能力不会让任何导航项消失，计进底栏那个数字只会
    // 让用户点进去发现「什么也没变」
    expect(ids).not.toContain("sound.audiobook")
    expect(ids).not.toContain("core.system")

    // 「被依赖而保留」（enabled=true）不算被关 —— 它还在导航里，不该出现在回头路入口
    expect(closedNavCapabilities(asCatalog({ "sound.tts": { state: "disabled" as const, enabled: true } }))).toHaveLength(0)
  })

  it("旧路由重定向全部来自清单", () => {
    resetPageCache()
    const { legacy } = buildRoutes(asCatalog())
    expect(legacy.map((l) => l.path).sort()).toEqual(
      MANIFEST.flatMap((p) => (p.legacy_routes ?? []).map((r) => r.path)).sort(),
    )
    expect(legacy.every((l) => l.redirect.startsWith("/"))).toBe(true)
  })
})
