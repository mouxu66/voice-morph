/**
 * `lib/modeSwitch.ts` —— 「切模式会关掉哪几项」的判据。
 *
 * 这是 2026-10-08 事故（能力面板开好 17 项 → 重启 → 被模式选择页整套覆盖）
 * 的**可断言核心**：确认条出现与否、点了会关掉什么，全由 `capabilitiesLostBy` 决定，
 * 所以它的边界要在这里钉死，而不是靠渲染测试间接碰运气。
 *
 * 钉四条：
 *  1. 只有「当前开着 + 不在套餐里 + 非 core」才计入（core 后端根本不给关）；
 *  2. 后端已展开过依赖闭包，所以「套餐内的 requires 依赖」会自然落在 `keep` 里，
 *     前端不需要也不该重算依赖；
 *  3. `null` / 未知套餐 id → 空数组（catalog 没加载完时**不猜**，宁可少问一次）；
 *  4. `describeLost` 超 6 个才折叠，且折叠后总数仍要报对（用户要判断的是规模）。
 */
import { describe, expect, it } from "vitest"
import type { PluginCatalog, PluginEntry, PluginPreset } from "@/types"
import { capabilitiesLostBy, describeLost } from "./modeSwitch"

function plugin(
  id: string,
  opts: { enabled?: boolean; core?: boolean; name?: string } = {},
): PluginEntry {
  return {
    id,
    name: opts.name ?? id,
    enabled: opts.enabled ?? true,
    core: opts.core ?? false,
  } as PluginEntry
}

function catalog(plugins: PluginEntry[], presets: PluginPreset[], preset = "custom"): PluginCatalog {
  return { plugins, presets, preset } as PluginCatalog
}

const PRESETS = [
  { id: "wechat", label: "微信变声", plugins: ["core.voices", "sound.offline-vc", "sound.tts"] },
  { id: "game", label: "游戏变声", plugins: ["core.voices", "sound.rvc-live"] },
  { id: "full", label: "全能", plugins: ["core.voices", "sound.offline-vc", "sound.tts"] },
] as PluginPreset[]

describe("capabilitiesLostBy", () => {
  it("只有「当前开着 + 不在套餐里」的才计入，套餐内的不算", () => {
    const cat = catalog(
      [
        plugin("core.voices", { core: true, name: "音色库" }),
        plugin("sound.tts", { name: "输字变声" }),
        plugin("sound.audiobook", { name: "有声书" }),
        plugin("sound.workshop", { name: "训练" }),
      ],
      PRESETS,
    )
    const lost = capabilitiesLostBy(cat, "wechat")
    expect(lost.map((p) => p.id)).toEqual(["sound.audiobook", "sound.workshop"])
  })

  it("已经关着的不算（它本来就不在跑，关它不损失什么）", () => {
    const cat = catalog(
      [plugin("sound.audiobook", { enabled: false, name: "有声书" }), plugin("sound.workshop")],
      PRESETS,
    )
    expect(capabilitiesLostBy(cat, "wechat").map((p) => p.id)).toEqual(["sound.workshop"])
  })

  it("core 即便漏在套餐外也不算 —— 后端根本不给关，报出来是假警报", () => {
    const cat = catalog([plugin("core.voices", { core: true })], [
      { id: "light", label: "轻量", plugins: [] } as PluginPreset,
    ])
    expect(capabilitiesLostBy(cat, "light")).toEqual([])
  })

  it("切到不关任何东西的套餐 → 空数组（这种情况不该弹确认）", () => {
    // 用户逐项开过之后又切回来：目标套餐含全部已开启项 → 没有损失
    const cat = catalog(
      [plugin("sound.tts"), plugin("sound.offline-vc"), plugin("core.voices", { core: true })],
      PRESETS,
    )
    expect(capabilitiesLostBy(cat, "full")).toEqual([])
  })

  it("catalog 没加载完 / 套餐 id 不认识 → 空数组，不猜", () => {
    const cat = catalog([plugin("sound.tts")], PRESETS)
    expect(capabilitiesLostBy(null, "wechat")).toEqual([])
    expect(capabilitiesLostBy(cat, "nope")).toEqual([])
  })
})

describe("describeLost", () => {
  it("≤6 个全列，>6 个折叠但报出总数", () => {
    expect(describeLost(["a", "b"])).toBe("a、b")
    expect(describeLost(["a", "b", "c", "d", "e", "f"])).toBe("a、b、c、d、e、f")
    expect(describeLost(["a", "b", "c", "d", "e", "f", "g", "h"])).toBe("a、b、c、d、e、f 等 8 项")
  })
})
