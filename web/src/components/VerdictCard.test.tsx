/**
 * `VerdictCard` 渲染测试 —— 守 P2-1 的**渲染侧**三条退化。
 *
 * 为什么值得测（这三条都不会报错，只会让用户看不到东西）：
 *   1. **「下一步」被吃掉**：这个卡片存在的意义就是告诉用户"接下来怎么办"。
 *      `actions` 少渲染一条、或整块被条件包错导致不出现，用户看到的就只是
 *      "素材有爆音"——然后呢？什么都不做。这是最严重的一种退化。
 *   2. **兜底色退化**：未知判定码落到 `unknown` 用灰色是对的，但如果 `bad`
 *      也渲染成灰色，"削波必须换素材"就看起来像"还行"。
 *   3. **数字重复**：`total=0` 时标题已经写了"没有人声"，数字行再说一次
 *      "没有切出切片"读起来像话没说完。
 *
 * 与 `lib/qualityVerdict.test.ts` 的分工：那个测**表**（判定码 ↔ 后端对账），
 * 这个测**表怎么落到 DOM 上**。
 */
import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { VerdictCard } from "./VerdictCard"
import type { PipelineVerdict } from "@/api/client"

function makeVerdict(over: Partial<PipelineVerdict> = {}): PipelineVerdict {
  return {
    verdict: "noisy",
    title: "切片里的伴奏/噪声压过了人声",
    detail: "40 条切片里只有 6 条可用，多数是「信噪比低 / 有效语音少」。",
    actions: ["用更干净的素材：录音时离麦克风近一点", "确认「去除背景音乐」这一步没有跳过"],
    ok_ratio: 0.15,
    total: 40,
    ok: 6,
    grades: { A: 3, B: 3, C: 0, D: 34 },
    reason: "noisy",
    ...over,
  }
}

describe("VerdictCard", () => {
  it("★ 「下一步」逐条渲染 —— 这是卡片存在的意义，少一条就是残废", () => {
    render(<VerdictCard v={makeVerdict()} />)
    expect(screen.getByText("下一步")).toBeInTheDocument()
    expect(screen.getByText(/用更干净的素材/)).toBeInTheDocument()
    expect(screen.getByText(/去除背景音乐/)).toBeInTheDocument()
  })

  it("结论与解释都出现（不能只给结论）", () => {
    render(<VerdictCard v={makeVerdict()} />)
    expect(screen.getByText("切片里的伴奏/噪声压过了人声")).toBeInTheDocument()
    expect(screen.getByText(/40 条切片里只有 6 条可用/)).toBeInTheDocument()
  })

  it("有切片时显示数字汇总", () => {
    render(<VerdictCard v={makeVerdict()} />)
    expect(screen.getByText(/40 条切片 · 可用 6 条（15%）/)).toBeInTheDocument()
  })

  it("★ total=0 时不重复说「没有切片」（标题已经说过了）", () => {
    render(
      <VerdictCard
        v={makeVerdict({ verdict: "empty", title: "这段素材里没有人声", total: 0, ok: 0, ok_ratio: 0 })}
      />,
    )
    expect(screen.getByText("这段素材里没有人声")).toBeInTheDocument()
    expect(screen.queryByText(/没有切出切片/)).not.toBeInTheDocument()
  })

  it("★ 判定等级落到 data-tone —— 红/黄/灰不能混（削波显示成灰色=误导）", () => {
    const cases: Array<[string, string]> = [
      ["ok", "ok"],
      ["too_short", "warn"],
      ["noisy", "warn"],
      ["too_loud", "bad"],
      ["wrong_speaker", "bad"],
      ["empty", "bad"],
      ["unknown", "muted"],
    ]
    for (const [code, tone] of cases) {
      const { container, unmount } = render(<VerdictCard v={makeVerdict({ verdict: code })} />)
      const el = container.querySelector("[data-verdict]")
      expect(el?.getAttribute("data-tone"), `${code} 的配色等级应是 ${tone}`).toBe(tone)
      unmount()
    }
  })

  it("未知判定码兜底成灰色，而不是崩/不渲染", () => {
    const { container } = render(<VerdictCard v={makeVerdict({ verdict: "某个后端新加的码" })} />)
    expect(container.querySelector("[data-tone='muted']")).toBeInTheDocument()
    // 兜底也要把后端给的 title/detail 原样显示出来 —— 有信息总比没有好
    expect(screen.getByText("切片里的伴奏/噪声压过了人声")).toBeInTheDocument()
  })

  it("actions 为空时不渲染「下一步」标题（空标题比没有更难看）", () => {
    render(<VerdictCard v={makeVerdict({ actions: [] })} />)
    expect(screen.queryByText("下一步")).not.toBeInTheDocument()
  })

  it("detail 为空时不渲染空段落", () => {
    const { container } = render(<VerdictCard v={makeVerdict({ detail: "" })} />)
    expect(container.querySelectorAll("p")).toHaveLength(2) // 标题 + 「下一步」小标题
  })

  it("给了 onDismiss 才有收起按钮，点了会回调", async () => {
    const onDismiss = vi.fn()
    const { rerender } = render(<VerdictCard v={makeVerdict()} />)
    expect(screen.queryByRole("button", { name: "收起这条结论" })).not.toBeInTheDocument()

    rerender(<VerdictCard v={makeVerdict()} onDismiss={onDismiss} />)
    const btn = screen.getByRole("button", { name: "收起这条结论" })
    btn.click()
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })
})
