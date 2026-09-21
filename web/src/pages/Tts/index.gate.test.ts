import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

/**
 * TTS 合并页的能力门控（C 类）：页内 tab 必须跟着 /api/plugins 走。
 *
 * 背景：hook.wechat / sound.audiobook 都是可关能力，关掉后后端不再挂载对应端点。
 * 若 tab 还留着，用户点进去就是一路 404。正解是按清单隐藏 tab，并让对应 hook
 * 停止轮询（端点不存在，轮询只会空转）。
 *
 * 本文件是「门控真的接上了」的静态门禁 —— 防止有人改了 hook/TsxRoute 写法、
 * 把 tab 重新变回无条件渲染。组件渲染测试在 jsdom 下才能跑，这里用源码断言
 * 与 petCapabilityGate.test.ts 同一手法。
 */

function readRel(rel: string): string {
  return readFileSync(path.join(process.cwd(), "src", rel), "utf8")
}

describe("TtsRoute 的 tab 门控", () => {
  const indexSrc = readRel(path.join("pages", "Tts", "index.tsx"))

  it("微信 tab 按 hook.wechat 显隐", () => {
    expect(indexSrc).toContain('pluginVisible(catalog, "hook.wechat")')
    expect(indexSrc).toContain("useWechatSend(wechatOn)")
    expect(indexSrc).toContain(`...(wechatOn`)
    expect(indexSrc).not.toContain(`{ key: "wechat", label: "微信发送", content: <WechatSendPage {...useWechatSend()} /> }`)
  })

  it("有声书 tab 按 sound.audiobook 显隐", () => {
    expect(indexSrc).toContain('pluginVisible(catalog, "sound.audiobook")')
    expect(indexSrc).toContain("useAudiobook(bookOn)")
    expect(indexSrc).toContain(`...(bookOn`)
    expect(indexSrc).not.toContain(`{ key: "book", label: "有声书（长文本）", content: <AudiobookPage {...useAudiobook()} /> }`)
  })

  it("hook 必须无条件调用（React 规则），显隐只发生在 tabs 数组里", () => {
    // 三行 hook 调用彼此相邻且在同一个函数体内（不在条件分支里）
    const calls = indexSrc.search(/const tts = useTts\(\)\n {2}const book = useAudiobook\(bookOn\)\n {2}const wechat = useWechatSend\(wechatOn\)/)
    expect(calls).toBeGreaterThanOrEqual(0)
    expect(indexSrc).toContain("const tts = useTts()")
  })
})

describe("被关掉时 hook 必须停止轮询（端点已不存在）", () => {
  it("useWechatSend 分量预热轮询与记录刷新", () => {
    const src = readRel(path.join("pages", "Tts", "useWechatSend.ts"))
    expect(src).toContain("if (!enabled || !backendUp)")
    expect(src).toContain("if (enabled && backendUp) void refresh()")
  })

  it("useAudiobook 挂载即查状态的分量在 enabled=false 时直接返回", () => {
    const src = readRel(path.join("pages", "Audiobook", "useAudiobook.ts"))
    expect(src).toContain("if (!enabled) return")
    expect(src).toContain("[enabled, refresh, stopPoll]")
  })
})