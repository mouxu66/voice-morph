import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

/**
 * 音色库页的能力门控（C 类补漏，2026-09-21）。
 *
 * 由来：C 类「UI 按清单门控」按 **page 粒度** 做了一遍，但漏了一种情况 ——
 * **核心页里嵌着可关能力的组件**。`/voices` 是 `core.voices`（不可关闭），所以
 * 它整页没做任何门控；可它同时又渲染了三个**可关能力**的面板：
 *
 *   · `AbCompareCard`  → `/api/ab/run`    ┐ sound.audition
 *   · `AbChainCard`    → `/api/ab/chain`  ┘（`routers: [ab_api, ab_chain, audition_api]`）
 *   · `AuditionEntry`  → 指向 `/audition` 路由 ── 同属 sound.audition
 *   · `WorksLibrary`   → `/api/history/*`  ── core.history
 *
 * 关掉 `sound.audition` 后，后端不再挂载这三个 router（以及 `/audition` 路由），
 * 但音色库里那几个面板照旧渲染 → 用户点一下就 404，且**看不出是为什么**
 * （核心页怎么会坏？）。这是「共享组件调用链没追」暴露出来的真实缺口。
 *
 * 本文件是静态门禁（与 `Tts/index.gate.test.ts` 同一手法）：组件渲染测试要 jsdom
 * 才能跑，这里直接断言源码，防止有人后续把门控改回无条件渲染。
 */

function readRel(rel: string): string {
  return readFileSync(path.join(process.cwd(), "src", rel), "utf8")
}

const pageSrc = readRel(path.join("pages", "Voices", "VoicesPage.tsx"))

describe("VoicesPage 里的可关能力面板按清单显隐", () => {
  it("取到了能力清单（core 页也得问一遍，页内组件可能属别的能力）", () => {
    expect(pageSrc).toContain('import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"')
    expect(pageSrc).toContain("usePluginCatalog()")
    expect(pageSrc).toContain('const catalog = catalogState.status === "ready" ? catalogState.catalog : null')
  })

  it("试音间的三个面板都挂在 sound.audition 上", () => {
    expect(pageSrc).toContain('pluginVisible(catalog, "sound.audition")')
    expect(pageSrc).toContain("{auditionOn && p.voices.length >= 2 && <AbCompareCard")
    expect(pageSrc).toContain("{auditionOn && p.voices.length >= 1 && <AbChainCard")
    expect(pageSrc).toContain('{auditionOn && <AuditionEntry')
  })

  it("作品库挂在 core.history 上", () => {
    expect(pageSrc).toContain('pluginVisible(catalog, "core.history")')
    expect(pageSrc).toContain("{historyOn && p.backendUp && <WorksLibrary")
  })

  it("★ 反例：这三个组件不许再出现无条件渲染", () => {
    // 门控是「加前缀」，删掉前缀就退回原样 —— 把原样钉死，改回去必红。
    // ⚠️ 判否定时必须带上前缀 `{`：门控后的写法是 `{auditionOn && p.voices.length >= 2 && ...`，
    // 它**包含** `p.voices.length >= 2 && <AbCompareCard` 这个子串 —— 只写后半段会永远命中、
    // 测试恒红（自己踩过一次）。所以锚点从「渲染表达式开头」整段取。
    expect(pageSrc).not.toContain("{p.voices.length >= 2 && <AbCompareCard")
    expect(pageSrc).not.toContain("{p.voices.length >= 1 && <AbChainCard")
    expect(pageSrc).not.toContain("{p.backendUp && <WorksLibrary")
    // AuditionEntry 原本没有 `{` 前缀（直接是 JSX 子节点），所以否定串要取「前面不是 &&」的形态：
    // 门控后是 `{auditionOn && <AuditionEntry hint=...`，用 `> <AuditionEntry` 这种
    // 「紧跟在闭合尖括号后」的形态才能区分开。
    expect(pageSrc).not.toContain('</div><AuditionEntry hint="音色挑好了，去试音一下？"')
  })
})

describe("门控变量确实来自清单，而不是硬编码", () => {
  it("没有把 auditionOn / historyOn 写成 true", () => {
    // 防「假门控」：声明了变量却恒为 true，测试看代码像接上了、行为上没接。
    expect(pageSrc).not.toMatch(/const\s+auditionOn\s*=\s*true/)
    expect(pageSrc).not.toMatch(/const\s+historyOn\s*=\s*true/)
  })

  it("catalog 拿不到时按可见处理（不替旧后端下结论）", () => {
    // pluginVisible 内部语义：catalog 为 null → true。调用点传的必须是可能为 null 的
    // catalog，不能是 `catalog!` / 直接解包 —— 那样老后端会整块消失。
    expect(pageSrc).toContain("pluginVisible(catalog,")
    expect(pageSrc).not.toContain("pluginVisible(catalog!,")
  })
})

describe("被门控的组件确实打对应后端（防止门控挂错对象）", () => {
  it("AbChainCard 打 /ab/chain，AbCompareCard 打 /ab/run", () => {
    const chain = readRel(path.join("components", "voice-studio", "AbChainCard.tsx"))
    const cmp = readRel(path.join("components", "voice-studio", "AbCompareCard.tsx"))
    expect(chain).toContain("abChainRun")
    expect(cmp).toContain("abRun")
  })

  it("WorksLibrary 打 /history/*", () => {
    const lib = readRel(path.join("components", "voice-studio", "WorksLibrary.tsx"))
    expect(lib).toContain("exportHistoryZip")
    expect(lib).toContain("listHistory")
  })

  it("ab_api / ab_chain 确实归 sound.audition；history_api 归 core.history", () => {
    // 门控 id 写错（比如把 core.history 写成 core.voices）这里会红。
    const repoRoot = path.resolve(process.cwd(), "..")
    const audition = JSON.parse(
      readFileSync(path.join(repoRoot, "m2_server/plugins/sound.audition/plugin.json"), "utf8"),
    ) as { routers: string[] }
    expect(audition.routers).toContain("ab_chain")
    expect(audition.routers).toContain("ab_api")

    const history = JSON.parse(
      readFileSync(path.join(repoRoot, "m2_server/plugins/core.history/plugin.json"), "utf8"),
    ) as { routers: string[] }
    expect(history.routers).toContain("history_api")
  })
})

/**
 * 第二组：**音色挖掘 / 音色工坊**（2026-09-21 补，同一类缺口的第二批）。
 *
 * `/voices` 归 `core.voices`（恒注册），但页内「音色挖掘」面板与「音色工坊」
 * 的导入链路分别打 `sound.mine` 与 `sound.workshop` 的端点 —— 两个都是**可关**的。
 * 这一组比第一组更隐蔽：`useVoices` 在**挂载时就轮询** `getMineState`，
 * 关掉挖掘后不但按钮 404，还会每 3 秒空转刷一次 404。所以门控必须一路收到 hook 里，
 * 不能只在 JSX 上藏按钮。
 */
describe("VoicesPage 的挖掘/工坊门控", () => {
  const routeSrc = readRel(path.join("pages", "Voices", "index.tsx"))
  const hookSrc = readRel(path.join("pages", "Voices", "useVoices.ts"))

  it("入口页读清单，两个开关都来自 pluginVisible（不是硬编码 true）", () => {
    expect(routeSrc).toContain('pluginVisible(catalog, "sound.mine")')
    expect(routeSrc).toContain('pluginVisible(catalog, "sound.workshop")')
    expect(routeSrc).not.toMatch(/const\s+mineOn\s*=\s*true/)
    expect(routeSrc).not.toMatch(/const\s+workshopOn\s*=\s*true/)
  })

  it("开关喂进了 hook（只在 JSX 上藏按钮不够）", () => {
    expect(routeSrc).toContain("useVoices(mineOn, workshopOn)")
    expect(hookSrc).toContain("export function useVoices(mineOn = true, workshopOn = true)")
  })

  it("★ 关掉时连轮询都不发（端点不存在，轮询只是空转刷 404）", () => {
    expect(hookSrc).toContain("if (!mineOn || !backendUp) return")
    // 挂载同步与 running 轮询两个 effect 都要挡
    expect(hookSrc.match(/if \(!mineOn \|\| !backendUp\) return/g)?.length).toBe(2)
    expect(hookSrc).toContain("[mineOn, mine.running, backendUp]")
  })

  it("动作层也挡一道（纵深防御：按钮不该在，但别指望 JSX 永远挡得住）", () => {
    expect(hookSrc).toContain("if (!mineOn) return")
    expect(hookSrc).toContain("if (!mineOn || !workshopOn) return")
  })

  it("导入链路要 mine + workshop（上传后先切片再挖）", () => {
    // 三条入口（文件夹 / 文件 / 录音）都汇到 ingestFiles，所以它就要两个开关
    expect(hookSrc).toContain("ingestOn: mineOn && workshopOn")
    expect(routeSrc).toContain("useVoices(mineOn, workshopOn)")
  })

  it("★ 反例：挖掘卡与侧栏指引不许再无条件渲染", () => {
    expect(pageSrc).not.toContain('<section className="space-y-10"><div className="rounded-2xl border border-border bg-card/85')
    expect(pageSrc).not.toContain('<div className="sticky top-[8.5rem] space-y-6"><div className="rounded-2xl border border-border bg-gradient-to-br from-primary/10')
    expect(pageSrc).toContain("{p.mineOn && <div")
    // ⚠️ 判否定要带上**前一个字符**：门控后的 `{p.ingestOn && <><div className="mt-3 …`
    // 仍然**包含** `<div className="mt-3 …` 这个子串，只写它永远命中、测试恒红。
    // 所以锚点取「紧跟在兄弟节点闭合之后」的形态（原本是 `</div><div className="mt-3 …`）。
    expect(pageSrc).not.toContain('</div><div className="mt-3 flex flex-wrap gap-2">')
    expect(pageSrc).toContain('{p.ingestOn && <><div className="mt-3 flex flex-wrap gap-2">')
  })

  it("门控 id 与被门控端点确实对得上", () => {
    const repoRoot = path.resolve(process.cwd(), "..")
    const mine = JSON.parse(
      readFileSync(path.join(repoRoot, "m2_server/plugins/sound.mine/plugin.json"), "utf8"),
    ) as { routers: string[]; requires: string[] }
    expect(mine.routers).toContain("mine_api")
    // mine 依赖 workshop：挖之前要先切片，所以关掉 workshop 时 mine 也不该单独可用
    expect(mine.requires).toContain("sound.workshop")

    const workshop = JSON.parse(
      readFileSync(path.join(repoRoot, "m2_server/plugins/sound.workshop/plugin.json"), "utf8"),
    ) as { routers: string[] }
    expect(workshop.routers).toContain("pipeline_api")
  })
})
