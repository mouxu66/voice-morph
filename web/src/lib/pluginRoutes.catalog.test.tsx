/**
 * 能力清单的**单一真相源**门禁（`usePluginCatalog` 的状态机）。
 *
 * 为什么单开一屏
 * --------------
 * `pluginRoutes.test.ts` 守的是「清单 → 路由/导航」的纯函数与跨语言契约，
 * 这里守的是**状态本身**：谁在什么时候把 loading/ready/error 写进去、谁会跟着更新。
 *
 * 真实事故（2026-09-21 用户截图，日志可查）
 * ----------------------------------------
 * 后端是主进程 spawn 出来的，而窗口紧接着就 loadFile 了 —— 前端的第一次
 * `/api/plugins` 会**赶在后端 bind 端口之前**发出去，必然失败。于是 App 与侧栏
 * `StudioNav` 同时进 error：主区域弹「读不到能力清单」+ 重试，侧栏写「导航暂不可用」。
 * 用户点了主区域的**重试**，主区域恢复了，**侧栏却永远停在 error** —— 因为
 * `reload()` 只 bump 自己那个 hook 实例的 nonce，模块级 cache 虽然被填上了，
 * 但别的实例的 `useEffect` 不会重跑，谁也不会再去读那份 cache。
 *
 * 症状就是：页面能用了，左侧导航一直是空的加一句「读不到能力清单，导航暂不可用」，
 * 看起来像「这个应用根本没接插件」。
 *
 * 所以下面两条必须同时成立（缺一条就会退回那个症状）：
 *   1. **所有消费者共享一份状态** —— 任何一个实例 reload，其余全部跟着恢复；
 *   2. **失败后自动重试**（有上限）—— 后端冷启动那几秒不该要用户手点，
 *      App 里 health/voices 是 5s 轮询自愈的，清单没道理不自愈。
 *
 * 注：这里的 `CATALOG` 只要求形状能过类型 —— 本屏断言的是状态迁移，不是 payload 解析，
 * 清单字段的解析与契约由 `pluginRoutes.test.ts` 用**真实 plugin.json** 覆盖。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const getPlugins = vi.fn()

vi.mock("@/api/client", () => ({
  getPlugins: (...a: unknown[]) => getPlugins(...a),
}))

import { CATALOG_RETRY, resetCatalogCache, usePluginCatalog } from "./pluginRoutes"
import type { PluginCatalog } from "@/types"

const CATALOG = {
  ok: true,
  counts: { total: 0, ok: 0, broken: 0, disabled: 0 },
  plugins: [],
  loaders: { routers: 0, loaded: 0, broken: [] },
  restartRequired: true,
  presets: [],
  preset: "full",
} as unknown as PluginCatalog

/** 一个消费者。`App.tsx` 与 `StudioNav.tsx` 就是同一 hook 的两个独立实例。 */
function Probe({ label }: { label: string }) {
  const { state, reload } = usePluginCatalog()
  return (
    <div>
      <span data-testid={`${label}:status`}>{state.status}</span>
      <span data-testid={`${label}:message`}>{state.status === "error" ? state.message : ""}</span>
      <button type="button" onClick={reload}>{`reload-${label}`}</button>
    </div>
  )
}

const statusOf = (label: string) => screen.getByTestId(`${label}:status`).textContent

beforeEach(() => {
  getPlugins.mockReset()
  resetCatalogCache()
})

afterEach(() => {
  // 先卸载再动共享状态：组件还挂着时改 state 会报 act 警告；
  // 顺手清掉可能在飞的自动重试定时器，别让它打到下一个用例里去
  cleanup()
  resetCatalogCache()
  vi.useRealTimers()
})

describe("清单状态是单一真相源（App 与侧栏不许各存一份）", () => {
  it("★ 一个实例 reload，另一个实例也得跟着恢复", async () => {
    // 首次失败 = 后端还没 bind 端口；重试成功 = 用户点了主区域的「重试」
    getPlugins.mockRejectedValueOnce(new Error("Failed to fetch"))
    getPlugins.mockResolvedValue(CATALOG)

    render(
      <>
        <Probe label="app" />
        <Probe label="nav" />
      </>,
    )

    await waitFor(() => expect(statusOf("app")).toBe("error"))
    expect(statusOf("nav")).toBe("error")

    fireEvent.click(screen.getByText("reload-app"))

    // 旧实现：nav 永远停在 error（这就是用户看到的那句「导航暂不可用」）
    await waitFor(() => expect(statusOf("nav")).toBe("ready"))
    expect(statusOf("app")).toBe("ready")
  })

  it("并发挂载只发一次请求（合流没被破坏）", async () => {
    getPlugins.mockResolvedValue(CATALOG)

    render(
      <>
        <Probe label="app" />
        <Probe label="nav" />
        <Probe label="page" />
      </>,
    )

    await waitFor(() => expect(statusOf("app")).toBe("ready"))
    expect(statusOf("nav")).toBe("ready")
    expect(statusOf("page")).toBe("ready")
    expect(getPlugins).toHaveBeenCalledTimes(1)
  })

  it("失败原因要能露到界面上（不能只剩一个 error）", async () => {
    getPlugins.mockRejectedValue(new Error("后端未启动"))
    render(<Probe label="app" />)
    await waitFor(() => expect(statusOf("app")).toBe("error"))
    expect(screen.getByTestId("app:message").textContent).toContain("后端未启动")
  })
})

describe("失败后自动重试（后端冷启动那几秒不该要用户手点）", () => {
  it("★ 首次失败后到点自己再拉一次，成功了就不用用户点重试", async () => {
    vi.useFakeTimers()
    getPlugins.mockRejectedValueOnce(new Error("Failed to fetch"))
    getPlugins.mockResolvedValue(CATALOG)

    render(<Probe label="app" />)
    await act(async () => {}) // 让第一次失败的 promise 落地
    expect(getPlugins).toHaveBeenCalledTimes(1)
    expect(statusOf("app")).toBe("error")

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CATALOG_RETRY.delayMs + 100)
    })
    expect(getPlugins).toHaveBeenCalledTimes(2)
    expect(statusOf("app")).toBe("ready")
  })

  it("重试有上限：一直起不来就停下来，留着「重试」按钮给用户", async () => {
    vi.useFakeTimers()
    getPlugins.mockRejectedValue(new Error("Failed to fetch"))

    render(<Probe label="app" />)
    await act(async () => {
      // 给足时间跑完整个预算（次数上限 × 间隔），再多给 10 倍也不该继续打
      await vi.advanceTimersByTimeAsync(CATALOG_RETRY.delayMs * CATALOG_RETRY.max * 10)
    })

    expect(getPlugins).toHaveBeenCalledTimes(1 + CATALOG_RETRY.max)
    expect(statusOf("app")).toBe("error")
  })

  it("用户手动重试会重新起一轮预算（不能因为之前用完就再也点不动）", async () => {
    vi.useFakeTimers()
    getPlugins.mockRejectedValue(new Error("Failed to fetch"))

    render(<Probe label="app" />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CATALOG_RETRY.delayMs * CATALOG_RETRY.max * 10)
    })
    const spent = getPlugins.mock.calls.length
    expect(spent).toBe(1 + CATALOG_RETRY.max)

    await act(async () => {
      fireEvent.click(screen.getByText("reload-app"))
    })
    expect(getPlugins).toHaveBeenCalledTimes(spent + 1)

    // ★ 关键：预算必须被重置 —— 点完还要能继续自动重试，
    //   否则用户每失败一次都得手点一次，等于预算用完就废了
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CATALOG_RETRY.delayMs + 100)
    })
    expect(getPlugins).toHaveBeenCalledTimes(spent + 2)
    expect(statusOf("app")).toBe("error")
  })

  it("拿到清单后不再自动重试（别把一次性读取变成轮询）", async () => {
    vi.useFakeTimers()
    getPlugins.mockResolvedValue(CATALOG)

    render(<Probe label="app" />)
    await act(async () => {})
    expect(statusOf("app")).toBe("ready")

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CATALOG_RETRY.delayMs * 5)
    })
    expect(getPlugins).toHaveBeenCalledTimes(1)
  })
})
