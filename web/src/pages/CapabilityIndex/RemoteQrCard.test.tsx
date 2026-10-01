/**
 * `RemoteQrCard` 四态 —— 守的是「手机遥控扫码即开」这条链路在界面上说得清。
 *
 * issue #1 改判后（mDNS 在浏览器端走不通），这个卡片就是「不用手填 IP」的全部。
 * 三个容易写错的点，各钉一条：
 * 1. 拿到 url 必须**原样展示**（含 `?api_key=`）—— 二维码内容与展示文本必须一致，
 *    否则「扫出来和看到的不一样」；
 * 2. 后端说没地址（ip=null）要给人话提示，不能渲染一个空卡片；
 * 3. token 未设置时后端的安全提醒（note）必须露出来。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const getLan = vi.fn()

vi.mock("@/api/client", () => ({
  getLan: (...a: unknown[]) => getLan(...a),
}))

import { RemoteQrCard } from "./RemoteQrCard"

afterEach(() => {
  getLan.mockReset()
  vi.useRealTimers()
})

function lan(overrides: Record<string, unknown> = {}) {
  return {
    ip: "192.168.1.5",
    port: 8000,
    url: "http://192.168.1.5:8000/remote.html",
    token_set: false,
    note: "未设置 VM_API_TOKEN：局域网内任意设备都可访问全部接口",
    ...overrides,
  }
}

describe("RemoteQrCard", () => {
  it("ready：地址原样展示 + 渲染出二维码 SVG + 提供复制", async () => {
    getLan.mockResolvedValue(lan())
    render(<RemoteQrCard />)
    await waitFor(() => {
      expect(screen.getByText("http://192.168.1.5:8000/remote.html")).toBeTruthy()
    })
    const qr = screen.getByRole("img", { name: "遥控页地址二维码" })
    // uqr renderSVG 生成的内联 SVG（白底黑块）—— 内容与展示文本同源
    expect(qr.querySelector("svg")).not.toBeNull()
    expect(screen.getByText("复制地址")).toBeTruthy()
    // token 未设置 → 后端的 note 原样露出
    expect(screen.getByText(lan().note)).toBeTruthy()
  })

  it("复制按钮写入剪贴板并给出已复制反馈", async () => {
    getLan.mockResolvedValue(lan())
    const write = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText: write } })
    render(<RemoteQrCard />)
    await screen.findByText("http://192.168.1.5:8000/remote.html")
    fireEvent.click(screen.getByText("复制地址"))
    await waitFor(() => expect(write).toHaveBeenCalledWith("http://192.168.1.5:8000/remote.html"))
    expect(screen.getByText("已复制")).toBeTruthy()
  })

  it("token_set：提示「地址里已带上访问密钥」，不露 note", async () => {
    getLan.mockResolvedValue(
      lan({ url: "http://192.168.1.5:8000/remote.html?api_key=t0k3n", token_set: true, note: "" }),
    )
    render(<RemoteQrCard />)
    await screen.findByText("http://192.168.1.5:8000/remote.html?api_key=t0k3n")
    expect(screen.getByText("地址里已带上访问密钥，别发到群里。")).toBeTruthy()
  })

  it("no-lan：ip=null 时给人话提示，不渲染二维码", async () => {
    getLan.mockResolvedValue(lan({ ip: null, url: null }))
    render(<RemoteQrCard />)
    await waitFor(() => {
      expect(screen.getByText(/没找到局域网地址/)).toBeTruthy()
    })
    expect(screen.queryByRole("img")).toBeNull()
  })

  it("error：请求失败露出错误信息，不渲染二维码", async () => {
    getLan.mockRejectedValue(new Error("后端没在跑"))
    render(<RemoteQrCard />)
    await waitFor(() => {
      expect(screen.getByText(/后端没在跑/)).toBeTruthy()
    })
    expect(screen.queryByRole("img")).toBeNull()
  })
})
