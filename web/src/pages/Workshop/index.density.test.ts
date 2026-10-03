import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

const hookSrc = readFileSync(
  path.resolve(__dirname, "useWorkshop.ts"),
  "utf-8",
)
const pageSrc = readFileSync(
  path.resolve(__dirname, "WorkshopPage.tsx"),
  "utf-8",
)

/**
 * 切片质检台的分批渲染门禁（2026-10-03）。
 *
 * 背景：切片走完流水线会有几百条（本机 487 条 / 43.7 分钟），每张卡都含一个
 * `<StudioAudioPlayer>`（内部一个 `<audio>`）。全量渲染实测把页面撑到
 * **38.2 屏**（1440px）/ **126 屏**（360px），一次挂 487 个媒体元素。
 *
 * 瓶颈**不在后端**：`/api/clips` 只要 0.24s / 132KB，且播放器懒加载 src
 * （实测 media 请求数 0）。所以后端分页对这个症状没用 —— 要限制的是 DOM 规模。
 *
 * 这个门禁钉两件事，缺一不可：
 *  1. 网格渲染 `shownClips`（分批切片），不是 `visibleClips`（全量）；
 *  2. **`visibleClips` 本身没被截断** —— 批量采纳/驳回、精审 A/R 快捷键、
 *     自动优选全读它。只截断渲染、不改数据源，是这个修法不偷换语义的关键。
 */
describe("切片质检台分批渲染", () => {
  it("网格渲染的是分批切片 shownClips，不是全量 visibleClips", () => {
    // 网格必须走 shownClips
    expect(pageSrc).toContain('data-testid="clip-grid"')
    expect(pageSrc).toContain("p.shownClips.map(")
    // 网格里不能再出现全量 map —— 否则 limit 白设
    expect(pageSrc).not.toContain("p.visibleClips.map(")
  })

  it("shownClips 是 visibleClips 的前缀切片（数据源不被截断）", () => {
    expect(hookSrc).toMatch(
      /shownClips\s*=\s*useMemo\(\(\)\s*=>\s*visibleClips\.slice\(0,\s*clipLimit\)/,
    )
    // hasMore 必须拿"总数 vs 已显示"比，而不是拿 visibleClips 的长度比
    expect(hookSrc).toContain("visibleClips.length > clipLimit")
  })

  it("续批入口存在，且如实告知剩余数量（不假装只有这些）", () => {
    expect(pageSrc).toContain("p.hasMoreClips")
    expect(pageSrc).toContain("p.showMoreClips")
    // 按钮上要写清"已显示 N / 总数"
    expect(pageSrc).toMatch(/已显示 \{p\.shownClips\.length\} \/ \{p\.visibleClips\.length\}/)
  })

  it("换筛选条件时把 limit 收回第一批", () => {
    // 否则「响度偏低」筛出 8 条后 limit 停在 150，切回「全部」会突然铺 150 张卡
    expect(hookSrc).toMatch(
      /setClipLimit\(CLIP_PAGE\)[\s\S]{0,80}\},\s*\[qualityFilter,\s*gradeFilter,\s*speakerFilter,\s*reviewMode\]\)/,
    )
  })

  it("批量操作与精审快捷键仍读全量 visibleClips（分页不影响语义）", () => {
    // markVisible 在页面里，遍历 visibleClips
    expect(pageSrc).toMatch(/markVisible[\s\S]{0,200}visibleClips\.forEach/)
    // 精审 A/R 快捷键在 hook 里，目标取自 visibleClips
    expect(hookSrc).toMatch(/visibleClips\.find\(\(c\)\s*=>\s*!decisions\[c\.name\]\)/)
    // 自动优选读 clips（全量数据），不是 shownClips
    expect(hookSrc).toMatch(/const autoPickFor[\s\S]{0,400}?clips\s*\n?\s*\.filter\(/)
  })
})
