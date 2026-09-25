import { readFileSync, readdirSync, existsSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

/**
 * 能力索引页（`pages/CapabilityIndex`）的门禁。
 *
 * 为什么需要它
 * ------------
 * 调研（`docs/调研-用户需求与界面精简-2026-09-25.md`）发现：侧栏只放 7 项，
 * 有 **10 个能力没有任何导航入口**，只能靠"碰巧滑到某个 tab"被发现。
 * 渐进式披露允许第三层功能不自动露出，但**前提是得有一条路径找得到它们** ——
 * 索引页就是那条路径。
 *
 * ★ 本文件防的回归
 * ---------------
 * 索引页是一张**手工维护的表**。手工表的宿命是漂移：后端加了新能力，
 * 前端忘了加进索引 → 它又变成"没有入口"，而且**没人会注意到**，
 * 因为现象是"某个东西找不到"而不是"某个东西报错"。
 *
 * 所以这里做**跨语言比对**：读后端 `m2_server/plugins` 下每个 plugin.json 里
 * 每条路由的 path，逐个确认索引页里出现过。与 `pluginRoutes.test.ts` 同一思路
 * （那边校验 icon 名与导出名，这边校验"路由都被索引收录了"）。
 *
 * 允许例外的理由要写进 `INTENTIONALLY_UNINDEXED`，**并说明原因** ——
 * 一个说不出原因的例外就等于门禁失效。
 */

const repoRoot = path.resolve(process.cwd(), "..")
const pluginsDir = path.join(repoRoot, "m2_server", "plugins")

/** 索引页源码（统一成 LF，避免 CRLF 让多行断言莫名红）。 */
const indexSrc = readFileSync(
  path.join(process.cwd(), "src", "pages", "CapabilityIndex", "CapabilityIndexPage.tsx"),
  "utf8",
).replace(/\r\n/g, "\n")

/**
 * 刻意不进索引的路由 + 原因。
 * ⚠️ 加条目必须写清理由，否则这条门禁就变成了"随手加白名单"。
 */
const INTENTIONALLY_UNINDEXED: Record<string, string> = {
  "/home": "首页本身就是落脚点，索引是为「找不到的东西」准备的",
  "/tools": "索引页自己不该自我引用",
}

describe("能力索引：没有能力被漏掉", () => {
  it("后端声明的每条路由都在索引页里出现过（或写明为何不收）", () => {
    expect(existsSync(pluginsDir), `找不到插件目录 ${pluginsDir}`).toBe(true)

    const paths: string[] = []
    for (const dir of readdirSync(pluginsDir)) {
      const file = path.join(pluginsDir, dir, "plugin.json")
      if (!existsSync(file)) continue
      const manifest = JSON.parse(readFileSync(file, "utf8")) as {
        routes?: { path?: string }[]
      }
      for (const r of manifest.routes ?? []) {
        if (r.path) paths.push(r.path)
      }
    }

    expect(paths.length, "一条路由都没读到，说明路径找错了").toBeGreaterThan(5)

    const missing = paths.filter(
      (p) => !(p in INTENTIONALLY_UNINDEXED) && !indexSrc.includes(`"${p}`),
    )
    expect(missing, `这些路由有入口但没进索引页：${missing.join(", ")}`).toEqual([])
  })

  it("索引里的每一项都过 pluginVisible（关掉的能力不能显示成可点链接）", () => {
    // 门控是逐项判断的，不能只在页面级做一次
    expect(indexSrc).toContain("pluginVisible(catalog, it.plugin)")
  })

  it("索引页不进主导航 —— 常驻侧栏会把刚做完的降噪还回去", () => {
    // 用"没有 nav 字段"表达：后端清单里 /tools 这条不该带 nav
    const manifest = JSON.parse(
      readFileSync(path.join(pluginsDir, "core.system", "plugin.json"), "utf8"),
    ) as { routes: { path: string; nav?: unknown }[] }
    const tools = manifest.routes.find((r) => r.path === "/tools")
    expect(tools, "core.system 里没有 /tools 路由").toBeTruthy()
    expect(tools?.nav, "/tools 不应该出现在主导航里").toBeUndefined()
  })

  it("侧栏底部有入口指向它（否则等于藏起来了）", () => {
    const chromeSrc = readFileSync(
      path.join(process.cwd(), "src", "components", "layout", "AppChrome.tsx"),
      "utf8",
    ).replace(/\r\n/g, "\n")
    expect(chromeSrc).toContain('to="/tools"')
    expect(chromeSrc).toContain("能做的事")
  })

  it("空清单要有说法（全被关掉时不能是一片空白）", () => {
    // 至少要有"少了哪一项 / 去能力管理"这类兜底文案
    expect(indexSrc).toContain("被关掉")
  })
})

describe("索引页的页面约定（与其它页一致）", () => {
  it("具名导出 <X>Route，与插件清单的 export 字段对得上", () => {
    const idx = readFileSync(
      path.join(process.cwd(), "src", "pages", "CapabilityIndex", "index.tsx"),
      "utf8",
    )
    expect(idx).toContain("CapabilityIndexRoute")
    const manifest = JSON.parse(
      readFileSync(path.join(pluginsDir, "core.system", "plugin.json"), "utf8"),
    ) as { routes: { path: string; module: string; export: string }[] }
    const tools = manifest.routes.find((r) => r.path === "/tools")
    expect(tools?.module).toBe("CapabilityIndex")
    expect(tools?.export).toBe("CapabilityIndexRoute")
  })
})
