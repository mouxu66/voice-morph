/**
 * 侧栏「使用模式」切换块（2026-10-03）。
 *
 * 守三条语义：
 *  1. 三个场景套餐渲染 + 当前模式高亮（数据以后端 catalog.preset 为准）；
 *  2. 点击 = 套用套餐 + 出现「重启后生效」横幅（套餐动整组，必须重启）；
 *  3. 非场景套餐（light/full）**不进侧栏** —— 侧栏只留「按用户是谁」分的三个场景，
 *     后端没返回场景套餐（旧后端）时整块隐藏，不给侧栏留一个死区块。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { getPlugins, applyPluginPreset, restartBackend } = vi.hoisted(() => ({
  getPlugins: vi.fn(),
  applyPluginPreset: vi.fn(),
  restartBackend: vi.fn(),
}))

vi.mock("@/api/client", () => ({
  getPlugins: (...a: unknown[]) => getPlugins(...a),
  applyPluginPreset: (...a: unknown[]) => applyPluginPreset(...a),
}))
vi.mock("@/lib/electron", () => ({
  restartBackend: (...a: unknown[]) => restartBackend(...a),
}))

import { ModeSwitcher } from "./ModeSwitcher"

function catalog(preset: string) {
  return {
    preset,
    presets: [
      { id: "wechat", label: "微信变声", plugins: ["sound.offline-vc", "sound.tts", "hook.wechat", "sound.fx-board"] },
      { id: "preserve", label: "声音保存（护嗓）", plugins: ["sound.preserve", "sound.tts", "sound.audiobook"] },
      { id: "game", label: "游戏变声", plugins: ["sound.offline-vc", "sound.rvc-live", "sound.audition"] },
      { id: "light", label: "轻量", plugins: ["sound.offline-vc"] },
      { id: "full", label: "全能", plugins: [] },
    ],
    plugins: [],
    counts: { total: 0, enabled: 0 },
  }
}

beforeEach(() => {
  vi.stubGlobal("location", { reload: vi.fn() })
  sessionStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe("ModeSwitcher", () => {
  it("渲染三个场景模式，当前模式高亮；轻量/全能不进侧栏", async () => {
    getPlugins.mockResolvedValue(catalog("preserve"))
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变声")).toBeTruthy())
    expect(screen.getByText("游戏变声")).toBeTruthy()
    // 声音保存是当前模式：按钮带 aria-pressed（active 由 className 表达，用 title 断言）
    expect(screen.getByTitle(/切换到「声音保存（护嗓）」.*（当前）/)).toBeTruthy()
    expect(screen.queryByTitle(/「轻量」/)).toBeNull()
    expect(screen.queryByTitle(/「全能」/)).toBeNull()
  })

  it("点击模式 → 默认只增不减（strict:false）+ 出现「重启后生效」横幅，且不弹确认", async () => {
    getPlugins.mockResolvedValue(catalog("custom"))
    applyPluginPreset.mockResolvedValue({ preset: "wechat", mode: "merge", disabled: [], enabled: [], kept: [] })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变声")).toBeTruthy())
    fireEvent.click(screen.getByTitle(/切换到「微信变声」/))
    // ★ 2026-10-15：默认不传 strict —— 点模式不关你手动开启的能力，所以无需确认
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("wechat", { strict: false }))
    expect(screen.getByText("重启后生效")).toBeTruthy()
    expect(screen.queryByRole("alert")).toBeNull()
  })

  /**
   * 只增不减的回执（2026-10-15）：用户此刻最想知道的就是「我开的东西掉没掉」。
   * 回执必须自己说出「保留了你手动开启的 N 项」—— 而不是让用户去能力面板里数。
   */
  it("只增不减的回执：保留了你手动开启的 N 项", async () => {
    getPlugins.mockResolvedValue({
      ...catalog("custom"),
      plugins: [
        { id: "sound.tts", name: "输字变声", enabled: true, core: false },
        { id: "sound.audiobook", name: "有声书", enabled: true, core: false },
      ],
    })
    applyPluginPreset.mockResolvedValue({
      preset: "game",
      mode: "merge",
      disabled: [],
      enabled: [],
      kept: ["sound.tts", "sound.audiobook"],
    })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变声")).toBeTruthy())
    fireEvent.click(screen.getByTitle(/切换到「游戏变声」/))
    await waitFor(() => expect(screen.getByText(/保留你手动开启的 2 项/)).toBeTruthy())
    expect(screen.queryByRole("alert")).toBeNull()
  })

  it("「立即重启」→ 调 restartBackend 并刷新页面", async () => {
    getPlugins.mockResolvedValue(catalog("custom"))
    applyPluginPreset.mockResolvedValue({ preset: "game", mode: "merge", disabled: [], enabled: [], kept: [] })
    restartBackend.mockResolvedValue({ running: true })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变声")).toBeTruthy())
    fireEvent.click(screen.getByTitle(/切换到「游戏变声」/))
    await waitFor(() => expect(screen.getByText("重启后生效")).toBeTruthy())
    fireEvent.click(screen.getByText("立即重启"))
    await waitFor(() => expect(restartBackend).toHaveBeenCalledTimes(1))
    expect(location.reload).toHaveBeenCalled()
    // ★ 2026-10-08 事故：这次重载必须压掉模式选择页，否则用户回来第一眼就是
    //   「今天怎么用？」，随手一点就把刚调好的开关整套覆盖
    expect(sessionStorage.getItem("vm.modechooser.suppress-once")).toBe("1")
  })

  /**
   * 2026-10-15：收窄变成**显式动作**。确认条从「点模式」搬家到卡片下面那行
   * 「严格套用（会关掉 N 项）」—— 只有它才会关掉你手动开启的能力。
   */
  it("严格套用才是会关东西的那一下 → 先确认，确认后带 strict=true", async () => {
    getPlugins.mockResolvedValue({
      ...catalog("custom"),
      plugins: [
        { id: "sound.tts", name: "输字变声", enabled: true, core: false },
        { id: "sound.audiobook", name: "有声书", enabled: true, core: false },
      ],
    })
    applyPluginPreset.mockResolvedValue({ preset: "game", mode: "merge", disabled: [], enabled: [], kept: [] })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变声")).toBeTruthy())

    // ① 点严格套用：不直接动，先点名将被关掉的那几项
    fireEvent.click(screen.getByTitle(/严格套用「游戏变声」/))
    expect(applyPluginPreset).not.toHaveBeenCalled()
    const alert = screen.getByRole("alert")
    expect(alert.textContent).toContain("关掉")
    expect(alert.textContent).toContain("2 项")
    expect(alert.textContent).toContain("输字变声")
    expect(screen.queryByText("重启后生效")).toBeNull() // 还没套用，自然不该有重启横幅

    // ② 确认后才走 strict
    fireEvent.click(screen.getByText("确认严格套用"))
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("game", { strict: true }))
  })

  it("严格套用的确认条可以取消 → 不套用、也不出现重启横幅", async () => {
    getPlugins.mockResolvedValue({
      ...catalog("custom"),
      plugins: [{ id: "sound.tts", name: "输字变声", enabled: true, core: false }],
    })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变声")).toBeTruthy())
    fireEvent.click(screen.getByTitle(/严格套用「游戏变声」/))
    fireEvent.click(screen.getByText("取消"))
    expect(screen.queryByRole("alert")).toBeNull()
    expect(screen.queryByText("重启后生效")).toBeNull()
    expect(applyPluginPreset).not.toHaveBeenCalled()
  })

  it("后端没返回场景套餐（旧后端/清单损坏）→ 整块隐藏", async () => {
    getPlugins.mockResolvedValue({
      preset: "standard",
      presets: [
        { id: "light", label: "轻量", plugins: ["sound.offline-vc"] },
        { id: "full", label: "全能", plugins: [] },
      ],
      plugins: [],
      counts: { total: 0, enabled: 0 },
    })
    const { container } = render(<ModeSwitcher />)
    await waitFor(() => expect(getPlugins).toHaveBeenCalled())
    expect(container.textContent).not.toContain("使用模式")
  })
})
