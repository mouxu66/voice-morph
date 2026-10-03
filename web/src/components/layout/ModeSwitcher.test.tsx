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
      { id: "wechat", label: "微信变身", plugins: ["sound.offline-vc", "sound.tts", "hook.wechat", "sound.fx-board"] },
      { id: "preserve", label: "声音保存（护嗓）", plugins: ["sound.preserve", "sound.tts", "sound.audiobook"] },
      { id: "game", label: "游戏变身", plugins: ["sound.offline-vc", "sound.rvc-live", "sound.audition"] },
      { id: "light", label: "轻量", plugins: ["sound.offline-vc"] },
      { id: "full", label: "全能", plugins: [] },
    ],
    plugins: [],
    counts: { total: 0, enabled: 0 },
  }
}

beforeEach(() => {
  vi.stubGlobal("location", { reload: vi.fn() })
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe("ModeSwitcher", () => {
  it("渲染三个场景模式，当前模式高亮；轻量/全能不进侧栏", async () => {
    getPlugins.mockResolvedValue(catalog("preserve"))
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变身")).toBeTruthy())
    expect(screen.getByText("游戏变身")).toBeTruthy()
    // 声音保存是当前模式：按钮带 aria-pressed（active 由 className 表达，用 title 断言）
    expect(screen.getByTitle(/切换到「声音保存（护嗓）」.*（当前）/)).toBeTruthy()
    expect(screen.queryByTitle(/「轻量」/)).toBeNull()
    expect(screen.queryByTitle(/「全能」/)).toBeNull()
  })

  it("点击模式 → 调 applyPluginPreset 并出现「重启后生效」横幅", async () => {
    getPlugins.mockResolvedValue(catalog("custom"))
    applyPluginPreset.mockResolvedValue({ ok: true, enabled: [] })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变身")).toBeTruthy())
    fireEvent.click(screen.getByTitle(/切换到「微信变身」/))
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("wechat"))
    expect(screen.getByText("重启后生效")).toBeTruthy()
  })

  it("「立即重启」→ 调 restartBackend 并刷新页面", async () => {
    getPlugins.mockResolvedValue(catalog("custom"))
    applyPluginPreset.mockResolvedValue({ ok: true, enabled: [] })
    restartBackend.mockResolvedValue({ running: true })
    render(<ModeSwitcher />)
    await waitFor(() => expect(screen.getByText("微信变身")).toBeTruthy())
    fireEvent.click(screen.getByTitle(/切换到「游戏变身」/))
    await waitFor(() => expect(screen.getByText("重启后生效")).toBeTruthy())
    fireEvent.click(screen.getByText("立即重启"))
    await waitFor(() => expect(restartBackend).toHaveBeenCalledTimes(1))
    expect(location.reload).toHaveBeenCalled()
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
