/**
 * 启动全屏「使用方式」选择器（2026-10-03）。
 *
 * 守五条语义：
 *  1. 非首次启动（UseCasePicker 已问过）才自动弹 —— 首次的全屏问答位让给首启组件；
 *  2. 选**不同**模式 = 套套餐 → 记目标页 → 重启后端 → 整页重载（路由/插件集要重启才挂上，
 *     2026-10-03 修的 bug：原先不重启就跳页，目标路由不存在被兜底弹回首页）；
 *  3. 选**当前**模式 = 不折腾，直接跳入口页；
 *  4. 重载后消费 pendingEntry（跳目标页不再弹）与 suppress（选完模式的重载只不弹）；
 *  5. 「记住我的选择」落盘，下次启动不弹。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const { hasBeenAsked, applyPluginPreset, restartBackend, navigate, reload, usePluginCatalog } = vi.hoisted(() => ({
  hasBeenAsked: vi.fn(),
  applyPluginPreset: vi.fn(),
  restartBackend: vi.fn(),
  navigate: vi.fn(),
  reload: vi.fn(),
  usePluginCatalog: vi.fn(),
}))

vi.mock("@/lib/useProfile", () => ({ hasBeenAsked: (...a: unknown[]) => hasBeenAsked(...a) }))
vi.mock("@/api/client", () => ({ applyPluginPreset: (...a: unknown[]) => applyPluginPreset(...a) }))
vi.mock("@/lib/electron", () => ({ restartBackend: (...a: unknown[]) => restartBackend(...a) }))
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
          { id: "wechat", label: "微信变声", plugins: ["sound.offline-vc", "sound.tts"] },
          { id: "preserve", label: "声音保存（护嗓）", plugins: ["sound.preserve", "sound.tts"] },
          { id: "game", label: "游戏变声", plugins: ["sound.offline-vc", "sound.rvc-live"] },
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
  sessionStorage.clear()
  vi.stubGlobal("location", { reload })
  usePluginCatalog.mockReturnValue(readyCatalog("custom"))
  applyPluginPreset.mockResolvedValue({ ok: true, enabled: [] })
  restartBackend.mockResolvedValue({ running: true })
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
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

  it("选不同模式 → 套套餐（只增不减）+ 记目标页 + 重启后端 + 重载（不直接 navigate）", async () => {
    hasBeenAsked.mockReturnValue(true)
    render(<ModeChooser />)
    fireEvent.click(screen.getByTitle(/启用 2 项能力，进入「声音保存」/))
    // ★ 默认 strict:false —— 只增不减，不问也不关
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("preserve", { strict: false }))
    await waitFor(() => expect(restartBackend).toHaveBeenCalledTimes(1))
    expect(localStorage.getItem("vm.pendingEntry")).toBe("/preserve")
    expect(sessionStorage.getItem("vm.modechooser.suppress-once")).toBe("1")
    // ★ 本页只出现一次：走过任何一个出口就落「问过了»
    expect(localStorage.getItem("vm.modechooser.dontask")).toBe("1")
    expect(reload).toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it("已是该模式 → 不套用不重启，直接跳入口页", () => {
    hasBeenAsked.mockReturnValue(true)
    usePluginCatalog.mockReturnValue(readyCatalog("game"))
    render(<ModeChooser />)
    fireEvent.click(screen.getByTitle(/进入「游戏变声」/))
    expect(applyPluginPreset).not.toHaveBeenCalled()
    expect(restartBackend).not.toHaveBeenCalled()
    expect(navigate).toHaveBeenCalledWith("/live")
  })

  /**
   * 2026-10-08 事故：本组件当时每次启动都弹，卡片点下去是**整套重设** ——
   * 用户在能力面板逐项开好的能力会在这里被一次点击清零。
   *
   * 2026-10-15 两道保险都上了：① 卡片默认**只增不减**（不再问、也不关）；
   * ② 想收窄走卡片下面那行显式的「严格套用」，确认后才走 strict。
   */
  function withOneManualCap() {
    const cat = readyCatalog("custom")
    cat.state.catalog.plugins = [
      { id: "sound.audiobook", name: "有声书", enabled: true, core: false },
    ] as never
    usePluginCatalog.mockReturnValue(cat)
  }

  it("点卡片不会关掉手动开启的能力：走只增不减，不弹确认", async () => {
    hasBeenAsked.mockReturnValue(true)
    withOneManualCap()
    render(<ModeChooser />)

    fireEvent.click(screen.getByTitle(/启用 2 项能力，进入「声音保存」/))
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("preserve", { strict: false }))
    // 没有东西会被夺走 → 不该有确认条（2026-10-15）
    expect(screen.queryByRole("alert")).toBeNull()
    expect(restartBackend).toHaveBeenCalledTimes(1)
    expect(reload).toHaveBeenCalled()
  })

  it("严格套用才确认：先点名将关掉的项，确认后带 strict=true", async () => {
    hasBeenAsked.mockReturnValue(true)
    withOneManualCap()
    render(<ModeChooser />)

    fireEvent.click(screen.getByTitle(/严格套用「声音保存」/))
    expect(applyPluginPreset).not.toHaveBeenCalled()
    const alert = screen.getByRole("alert")
    expect(alert.textContent).toContain("关掉")
    expect(alert.textContent).toContain("有声书")

    fireEvent.click(screen.getByText("确认严格套用并重启"))
    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("preserve", { strict: true }))
    await waitFor(() => expect(restartBackend).toHaveBeenCalledTimes(1))
    expect(reload).toHaveBeenCalled()
  })

  it("严格套用的确认条可以取消 → 不套用、不重启、页面不重载", () => {
    hasBeenAsked.mockReturnValue(true)
    const cat = readyCatalog("custom")
    cat.state.catalog.plugins = [
      { id: "sound.audiobook", name: "有声书", enabled: true, core: false },
    ] as never
    usePluginCatalog.mockReturnValue(cat)
    render(<ModeChooser />)

    fireEvent.click(screen.getByTitle(/严格套用「声音保存」/))
    fireEvent.click(screen.getByText("取消"))
    expect(screen.queryByRole("alert")).toBeNull()
    expect(applyPluginPreset).not.toHaveBeenCalled()
    expect(restartBackend).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
  })

  it("重载后消费 pendingEntry → 跳目标页且不再弹；一次性 suppress 同样只不弹", () => {
    hasBeenAsked.mockReturnValue(true)
    localStorage.setItem("vm.pendingEntry", "/preserve")
    render(<ModeChooser />)
    expect(navigate).toHaveBeenCalledWith("/preserve")
    expect(localStorage.getItem("vm.pendingEntry")).toBeNull()
    expect(screen.queryByRole("dialog", { name: "选择使用方式" })).toBeNull()

    // suppress：无 pendingEntry 时只关闭弹窗（刚选完模式的重载）
    sessionStorage.setItem("vm.modechooser.suppress-once", "1")
    const { container } = render(<ModeChooser />)
    expect(navigate).toHaveBeenCalledTimes(1) // 没有再跳
    expect(container.textContent).not.toContain("选择一个使用方式")
    expect(sessionStorage.getItem("vm.modechooser.suppress-once")).toBeNull()
  })

  it("「直接进入」→ 什么都不做只关闭；且这页只出现一次（直接落「问过了»）", () => {
    hasBeenAsked.mockReturnValue(true)
    render(<ModeChooser />)
    fireEvent.click(screen.getByText("直接进入"))
    expect(navigate).not.toHaveBeenCalled()
    expect(applyPluginPreset).not.toHaveBeenCalled()
    expect(restartBackend).not.toHaveBeenCalled()

    // ★ 2026-10-15：不再是「勾了才记住」—— 三个出口都落盘，本页只出现一次
    expect(localStorage.getItem("vm.modechooser.dontask")).toBe("1")
    expect(shouldShowModeChooser()).toBe(false)
  })
})
