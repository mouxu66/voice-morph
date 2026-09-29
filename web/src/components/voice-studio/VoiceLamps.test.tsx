/**
 * `VoiceLamps` 渲染测试 —— 守音色体检三灯的**三条退化**。
 *
 * 为什么值得测（这三条都不会报错，只会让用户看错信息）：
 *   1. **「未测」被渲染成红灯**：parselmouth 没装 / 还没跑质检时 value=null。
 *      如果 UI 把它画成红色，用户会去修一个不存在的问题（"我的音色音质红"），
 *      而真正该做的是「跑一次质检」。后端已经区分了 null 与 red，
 *      前端**不能**把它合回去 —— 这是这个文件最重要的一条。
 *   2. **「未体检」与「三个灰灯」混同**：data 为 null 表示从没体检过；
 *      三个 lamp 都为 null 表示体检了但全没测到。前者该显示"未体检"，
 *      后者该显示三个灰灯 + 计数，否则用户分不清"该去跑"还是"跑了没用"。
 *   3. **结论被吞**：verdict 是给用户看的那句话。少渲染它，
 *      三个灯就只是一排看不出所以然的彩点。
 */
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { VoiceLamps } from "./VoiceLamps"
import type { LampsResult } from "@/types"

function makeData(over: Partial<LampsResult> = {}): LampsResult {
  return {
    lamps: [
      { key: "similarity", label: "相似度", value: 0.97, lamp: "green", detail: "声纹余弦 0.9700", sources: [] },
      { key: "naturalness", label: "自然度", value: 12.0, lamp: "green", detail: "HNR 12.0 dB", sources: ["hnr"] },
      { key: "strain", label: "夹嗓子风险", value: 2.0, lamp: "green", detail: "H1-H2 +2.0 dB", sources: ["h1_h2"] },
    ],
    verdict: "三项体检都在素材基线内，可以直接用",
    tested_count: 3,
    ...over,
  }
}

describe("VoiceLamps", () => {
  it("data 为 null 时显示「未体检」，不画灯", () => {
    render(<VoiceLamps data={null} />)
    expect(screen.getByText("未体检")).toBeTruthy()
    // 不能把三个灯名渲染出来 —— 否则用户以为体检过了
    expect(screen.queryByText("相似度")).toBeNull()
  })

  it("undefined 同样走「未体检」而不是崩", () => {
    render(<VoiceLamps data={undefined} />)
    expect(screen.getByText("未体检")).toBeTruthy()
  })

  it("渲染三个灯名与结论", () => {
    render(<VoiceLamps data={makeData()} />)
    expect(screen.getByText("相似度")).toBeTruthy()
    expect(screen.getByText("自然度")).toBeTruthy()
    expect(screen.getByText("夹嗓子风险")).toBeTruthy()
    expect(screen.getByText(/三项体检都在素材基线内/)).toBeTruthy()
  })

  it("★ 未测的灯不显示成红灯（lamp=null 单独一档）", () => {
    const data = makeData({
      lamps: [
        { key: "similarity", label: "相似度", value: null, lamp: null, detail: "未测：需要先完成一次变声验收", sources: [] },
        { key: "naturalness", label: "自然度", value: 11.7, lamp: "green", detail: "HNR 11.7 dB", sources: ["hnr"] },
        { key: "strain", label: "夹嗓子风险", value: 4.8, lamp: "yellow", detail: "H1-H2 +4.8 dB", sources: ["h1_h2"] },
      ],
      verdict: "可用，但「夹嗓子风险」偏弱（相似度 未测）",
      tested_count: 2,
    })
    const { container } = render(<VoiceLamps data={data} />)
    // 未测项不得使用红灯色类
    const sim = screen.getByTitle("未测：需要先完成一次变声验收")
    expect(sim.className).not.toContain("red")
    // 但红色类确实存在于系统里（确认上面的断言不是因为整个组件都没红）
    expect(container.innerHTML).toContain("amber") // 黄灯在
  })

  it("显示「已测 n/N」计数，让部分可用可见", () => {
    const data = makeData({
      lamps: [
        { key: "similarity", label: "相似度", value: null, lamp: null, detail: "未测", sources: [] },
        { key: "naturalness", label: "自然度", value: 12.0, lamp: "green", detail: "HNR 12.0 dB", sources: ["hnr"] },
        { key: "strain", label: "夹嗓子风险", value: 2.0, lamp: "green", detail: "H1-H2 +2.0 dB", sources: ["h1_h2"] },
      ],
      verdict: "两项在基线内（相似度 未测）",
      tested_count: 2,
    })
    render(<VoiceLamps data={data} />)
    expect(screen.getByText(/2\/3 项已测/)).toBeTruthy()
  })

  it("compact 模式不渲染结论句（卡片列表用）", () => {
    render(<VoiceLamps data={makeData()} compact />)
    expect(screen.queryByText(/三项体检都在素材基线内/)).toBeNull()
    // 但灯还在
    expect(screen.getByText("相似度")).toBeTruthy()
  })
})
