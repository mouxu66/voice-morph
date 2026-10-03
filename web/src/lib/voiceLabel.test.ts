/**
 * voiceSourceTag / voiceOptionLabel 三态来源标签（2026-10-03）。
 *
 * 「声音保存」是比赛主打能力：复现音色的 source=preserve（档案 meta.json 透传），
 * 标签若错标成「自训」，评委在输字页看到的来源就和演示故事对不上。
 * 判定入口只有这一个函数 —— 改标签语义只动这里。
 */
import { describe, expect, it } from "vitest"
import { voiceOptionLabel, voiceSourceTag } from "./voiceLabel"

describe("voiceSourceTag", () => {
  it("market → 市场", () => {
    expect(voiceSourceTag({ source: "market" })).toBe("市场")
  })

  it("preserve → 声音保存", () => {
    expect(voiceSourceTag({ source: "preserve" })).toBe("声音保存")
  })

  it("空串 / 缺字段 / 未知值（mine 等）→ 自训", () => {
    expect(voiceSourceTag({ source: "" })).toBe("自训")
    expect(voiceSourceTag({})).toBe("自训")
    expect(voiceSourceTag({ source: "mine" })).toBe("自训")
  })
})

describe("voiceOptionLabel", () => {
  it("复现音色 → 王老师（声音保存）", () => {
    expect(voiceOptionLabel({ id: "preserve_p1", display_name: "王老师", source: "preserve" }))
      .toBe("王老师（声音保存）")
  })

  it("市场音色 → 卡通·懒羊羊（市场）", () => {
    expect(voiceOptionLabel({ id: "katoong", display_name: "卡通·懒羊羊", source: "market" }))
      .toBe("卡通·懒羊羊（市场）")
  })

  it("无名字时退回 id", () => {
    expect(voiceOptionLabel({ id: "plain", source: "" })).toBe("plain（自训）")
  })
})
