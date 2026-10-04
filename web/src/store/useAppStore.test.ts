import { beforeEach, describe, expect, it } from "vitest"

import { useAppStore } from "@/store/useAppStore"

/**
 * `syncSelectedVoice` 的语义守卫。
 *
 * 背景：`selectedVoiceId` 其实有**两个来源** ——
 *   1. 后端 `media/voicebank/selected_voice.json`（音色挖掘 / 声音保存时落盘）；
 *   2. 用户在本界面下拉 / 音色卡上的手动选择。
 * 启动轮询（每 5s）负责把 (1) 同步进来，但它**绝不能盖掉** (2)。
 *
 * 少了这个函数时线上是真的坏的：store 初值 null，且只有手动选择才会写 →
 * 「后端明明有选中音色，前端认为一个都没选」→ 快捷短语板底部的橙色提示常亮、
 * 录课页「开始合成」永久变灰。
 *
 * ⚠️ 注意它**不是**「短语板不出声」的原因（那条是 main.cjs 的键位问题：
 * 裸 Alt+N 被菜单栏吃掉）。`play()` 照发请求、后端用自己选中的音色兜底 ——
 * 别再把两件事混成一件（2026-10-04 我错过一次）。
 */
describe("useAppStore.syncSelectedVoice", () => {
  beforeEach(() => {
    useAppStore.setState({ selectedVoiceId: null })
  })

  it("还没选时，接受后端给的音色（这是修好播放的关键路径）", () => {
    useAppStore.getState().syncSelectedVoice("kangaroo")
    expect(useAppStore.getState().selectedVoiceId).toBe("kangaroo")
  })

  it("用户已经选过时，后端值让路 —— 否则轮询每 5s 打回旧值", () => {
    useAppStore.getState().selectVoice("aishell3_ssb0407")
    useAppStore.getState().syncSelectedVoice("kangaroo")
    expect(useAppStore.getState().selectedVoiceId).toBe("aishell3_ssb0407")
  })

  it("后端返回空串时忽略 —— 不能把用户选好的音色清掉", () => {
    useAppStore.getState().selectVoice("kangaroo")
    useAppStore.getState().syncSelectedVoice("")
    expect(useAppStore.getState().selectedVoiceId).toBe("kangaroo")
  })

  it("还没选 + 后端也为空 → 保持 null（不要写成空串，两者语义不同）", () => {
    useAppStore.getState().syncSelectedVoice("")
    expect(useAppStore.getState().selectedVoiceId).toBeNull()
  })

  it("反复同步同一个值是幂等的（5s 轮询会跑很多次）", () => {
    useAppStore.getState().syncSelectedVoice("kangaroo")
    useAppStore.getState().syncSelectedVoice("kangaroo")
    useAppStore.getState().syncSelectedVoice("kangaroo")
    expect(useAppStore.getState().selectedVoiceId).toBe("kangaroo")
  })

  it("轮到不同值时能让路（场景：用户在别处换了音色，本界面还没选过）", () => {
    useAppStore.getState().syncSelectedVoice("kangaroo")
    // 此时已非空 → 后续同步让路，保持第一个值
    useAppStore.getState().syncSelectedVoice("aishell3_ssb0407")
    expect(useAppStore.getState().selectedVoiceId).toBe("kangaroo")
  })
})
