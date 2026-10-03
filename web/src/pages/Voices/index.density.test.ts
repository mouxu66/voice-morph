import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

/**
 * 音色库页的**首屏体量**门禁（2026-10-03）。
 *
 * 由来：前端体检报出「我的音色」页 6.7 屏 / 147 个按钮，密度失控。
 * 但实测拆开看，结论和最初的判断不一样 —— **不是「分区太多」**：
 *
 *   区块                     高度        占比
 *   WorksLibrary（作品库）   4.17 屏     79%   ← 158 个控件里 125 个在这
 *   建立新档案（右栏）       1.00 屏      —
 *   其它全部合计             1.23 屏      —
 *
 * 也就是说「太满」这件事几乎全部由**一个面板**造成：作品库是**产出归档浏览器**
 * （能翻页、每页 30 条、每条带播放器/收藏/标签/勾选），而它在本页的角色是
 * 附属 —— 用户进「我的音色」是为了管音色档案，不是为了翻作品。
 *
 * 所以处置不是「重新分区」，而是把这一个面板收进 `AdvancedSection`
 * （项目里已有的渐进式披露组件，语义是「先收着、想看随时点」，不是删内容）。
 * 实测 6.4 屏 → 2.3 屏、158 → 34 个控件；展开后 5858px / 46 条全部可达。
 *
 * 本文件是**静态门禁**，与同目录 `index.gate.test.ts` 同一手法：
 * 钉住「作品库必须待在 AdvancedSection 里」这个结构约定 ——
 * 体量问题不写测试，下次重构就会有人把它挪回主列首屏（而 diff 看起来完全正常）。
 */

function readRel(rel: string): string {
  return readFileSync(path.join(process.cwd(), "src", rel), "utf8")
}

const pageSrc = readRel(path.join("pages", "Voices", "VoicesPage.tsx"))

describe("音色库页首屏体量", () => {
  it("作品库收在 AdvancedSection 里（它是 4.17 屏 / 79% 控件的来源）", () => {
    expect(pageSrc).toContain(
      '<AdvancedSection summary="作品库"',
    )
    expect(pageSrc).toMatch(
      /\{historyOn && p\.backendUp && <AdvancedSection[^>]*>\s*<WorksLibrary voices=\{p\.voices\} \/>\s*<\/AdvancedSection>\}/,
    )
  })

  it("不许把作品库挪回主列首屏的无条件渲染", () => {
    // 这就是修复前的形态：直接摊在主列里
    expect(pageSrc).not.toContain("{historyOn && p.backendUp && <WorksLibrary voices={p.voices} />}")
  })

  it("AdvancedSection 已导入（别只改 JSX 忘了 import）", () => {
    expect(pageSrc).toContain('import { AdvancedSection } from "@/components/AdvancedSection"')
  })

  it("core.history 门控仍然在（折叠不能顺手把能力门控删了）", () => {
    // 折叠的是「默认展开与否」，不是「有没有权限渲染」——
    // 关掉 core.history 时依然要整块消失。
    expect(pageSrc).toContain('pluginVisible(catalog, "core.history")')
    expect(pageSrc).toContain("historyOn && p.backendUp && <AdvancedSection")
  })
})
