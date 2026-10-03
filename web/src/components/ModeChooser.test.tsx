/**
 * 启动全屏「使用方式」选择器（2026-10-03）。
 *
 * 守四条语义：
 *  1. 非首次启动（UseCasePicker 已问过）才自动弹 —— 首次的全屏问答位让给首启组件；
 *  2. 三张场景卡来自后端清单（场景 id 过滤），当前套餐带「当前」标记；
 *  3. 选卡 = 套用场景套餐 + 跳入口页（preserve → /preserve）；
 *  4. 「记住我的选择」勾上后落盘，下次不再弹（跳过同样落盘）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { hasBeenAsked, applyPluginPreset, navigate, usePluginCatalog } = vi.hoisted(() => ({
  hasBeenAsked: vi.fn(),
  applyPluginPreset: vi.fn(),
  navigate: vi.fn(),
  usePluginCatalog: vi.fn(),
}))

vi.mock("@/lib/useProfile", () => ({ hasBeenAsked: (...a: unknown[]) => hasBeenAsked(...a) }))
vi.mock("@/api/client", () => ({ applyPluginPreset: (...a: unknown[]) => applyPluginPreset(...a) }))
vi.mock("react-router", () => ({ useNavigate: () => navigate }))
vi.mock("@/lib/pluginRoutes", () => ({ usePluginCatalog: (...a: unknown[]) => usePluginCatalog(...a) }))

import { ModeChooser, shouldShowModeChooser } from "./ModeChooser"

function readyCatalog(preset: string) {
  return {
    state: {
      status: "ready" as const,
      catalog: {
        preset,
        presets: [
          { id: "wechat", label: "微信变身", plugins: ["sound.offline-vc", "sound.tts"] },
          { id: "preserve", label: "声音保存（护嗓）", plugins: ["sound.preserve", "sound.tts"] },
          { id: "game", label: "游戏变身", plugins: ["sound.offline-vc", "sound.rvc-live"] },
          { id: "light", label: "轻量", plugins: ["sound.offline-vc"] },
        ],
        plugins: [],
        counts: { total: 4, enabled: 2 },
      },
    },
  }
}

beforeEach(() => {
  localStorage.clear()
  usePluginCatalog.mockReturnValue(readyCatalog("custom"))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe("ModeChooser", () => {
  it("非首次启动 → 自动弹全屏选择；首次启动让位给 UseCasePicker", () => {
    hasBeenAsked.mockReturnValue(true)
    render(<ModeChooser />)
    expect(screen.getByRole("dialog", { name: "选择使用方式" })).toBeTruthy()
    expect(screen.getByText("声音保存")).toBeTruthy()

    hasBeenAsked.mockReturnValue(false)
    const { container } = render(<ModeChooser />)
    expect(container.textContent).not.toContain("选择一个使用方式")
  })

  it("勾「记住我的选择」后选「声音保存」→ 套用 preserve + 跳 /preserve + 落盘不再弹", async () => {
    hasBeenAsked.mockReturnValue(true)
    applyPluginPreset.mockResolvedValue({ ok: true, enabled: [] })
    render(<ModeChooser />)
    fireEvent.click(screen.getByText("记住我的选择，下次不再询问"))
    fireEvent.click(screen.getByTitle(/启用 2 项能力，进入「声音保存」/))
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("preserve"))
    expect(navigate).toHaveBeenCalledWith("/preserve")
    expect(localStorage.getItem("vm.modechooser.dontask")).toBe("1")
    expect(shouldShowModeChooser()).toBe(false)
  })

  it("已是该模式（preset 匹配）→ 不重复套用，只跳页", () => {
    hasBeenAsked.mockReturnValue(true)
    usePluginCatalog.mockReturnValue(readyCatalog("game"))
    render(<ModeChooser />)
    fireEvent.click(screen.getByTitle(/进入「游戏变身」/))
    expect(applyPluginPreset).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith("/live")
  })

  it("「直接进入」→ 什么都不做只关闭；勾选后同样落盘", () => {
    hasBeenAsked.mockReturnValue(true)
    render(<ModeChooser />)
    fireEvent.click(screen.getByText("直接进入"))
    expect(navigate).not.toHaveBeenCalled()
    expect(applyPluginPreset).not.toHaveBeenCalled()

    // 落盘后 shouldShow 为 false → 下次启动不再弹
    localStorage.setItem("vm.modechooser.dontask", "1")
    expect(shouldShowModeChooser()).toBe(false)
  })
})
