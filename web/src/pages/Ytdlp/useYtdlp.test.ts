import { describe, expect, it, vi } from "vitest"
import { act, renderHook, waitFor } from "@testing-library/react"

import { useYtdlp } from "@/pages/Ytdlp/useYtdlp"

/**
 * `useYtdlp` 的探活行为。
 *
 * 这个 hook 值得单测的只有一处：**重新检测**（`reprobe`）。它存在的理由是
 * 一条真实用户路径 —— "进来看到没装 → 去装 → 回到页面" —— 装完不重启后端
 * 也得能立刻看到状态变化。如果 `reprobe` 只是重新 setState 而不重发请求，
 * 这条路径就断了，而且断得很隐蔽（按钮有反应、状态就是不变）。
 */

const READY = {
  available: true,
  path: "C:\\x\\yt-dlp.exe",
  version: "2026.03.17",
  sites: [{ name: "网易云音乐", example: "music.163.com" }],
  // 登录态默认"未配" —— 后端不设环境变量时就是这个值（匿名请求）
  cookie: { mode: "none" as const, detail: "", ok: true },
  hint: "",
}

const MISSING = { ...READY, available: false, path: "", version: "", hint: "没找到 yt-dlp" }

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client")
  return {
    ...actual,
    ytdlpStatus: vi.fn(),
    fetchYtdlp: vi.fn(),
    mediaUrl: (u: string) => u,
  }
})

const client = await import("@/api/client")
const ytdlpStatus = vi.mocked(client.ytdlpStatus)
const fetchYtdlp = vi.mocked(client.fetchYtdlp)

describe("useYtdlp · 探活", () => {
  it("进页面就查一次，并把结果映射成 ready", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    const { result } = renderHook(() => useYtdlp())

    await waitFor(() => expect(result.current.probing).toBe(false))
    expect(result.current.ready).toBe(true)
    expect(result.current.status?.version).toBe("2026.03.17")
    expect(ytdlpStatus).toHaveBeenCalledTimes(1)
  })

  it("★ reprobe 要真的重发请求 —— 装完 yt-dlp 不重启也能看到变化", async () => {
    ytdlpStatus.mockResolvedValueOnce(MISSING)
    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    expect(result.current.ready).toBe(false)

    // ⚠️ 不能断言"总共调了 2 次"：StrictMode 下 effect 会跑两遍，次数取决于
    // 测试环境的 strict 配置 —— 那是在测 React，不是测本 hook。
    // 改为记录**调用基线**：reprobe 之后次数必须真的增加。
    const before = ytdlpStatus.mock.calls.length

    // 用户去装了工具，后端现在能找到了
    ytdlpStatus.mockResolvedValue(READY)
    await act(async () => {
      result.current.reprobe()
    })
    await waitFor(() => expect(result.current.ready).toBe(true))

    expect(ytdlpStatus.mock.calls.length).toBeGreaterThan(before)
  })

  it("探活抛错时降级为 not ready，而不是把异常抛给页面", async () => {
    ytdlpStatus.mockRejectedValue(new Error("后端还没起来"))
    const { result } = renderHook(() => useYtdlp())

    await waitFor(() => expect(result.current.probing).toBe(false))
    expect(result.current.ready).toBe(false)
    expect(result.current.status).toBeNull()
    // 探活失败不该污染错误区 —— 那是留给"拉取失败"的，见 hook 里的注释
    expect(result.current.errorMessage).toBe("")
  })
})

describe("useYtdlp · 拉取", () => {
  it("把后端规范化之后的地址透出来（粘短链时和输入不同）", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    fetchYtdlp.mockResolvedValue({
      ok: true,
      name: "ytdlp_1.mp3",
      url: "/media/session/ytdlp_1.mp3",
      bytes: 1024,
      site: "QQ音乐",
      // 后端把 `c6.y.qq.com/base/fcgi-bin/u?__=…` 规范化成了这个
      source_url: "https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo",
      duration_s: 152,
    })

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))

    await act(async () => {
      result.current.setUrl("https://c6.y.qq.com/base/fcgi-bin/u?__=yY3vbmLH9kYO")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    expect(result.current.fetchedSite).toBe("QQ音乐")
    // 页面靠"它 != 提交时那条"来决定要不要多显示一行 —— 所以这一项不能被丢掉，
    // 而且必须是**提交时**的输入（不是输入框当前值：用户结果出来后常接着改输入框）。
    expect(result.current.submittedUrl).toBe("https://c6.y.qq.com/base/fcgi-bin/u?__=yY3vbmLH9kYO")
    expect(result.current.fetchedSourceUrl).toBe("https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo")
    expect(result.current.fetchedSourceUrl).not.toBe(result.current.submittedUrl)
  })

  it("★ 拉取失败要把上一条成功的预览清掉", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    fetchYtdlp.mockResolvedValueOnce({
      ok: true,
      name: "a.mp3",
      url: "/media/session/a.mp3",
      bytes: 10,
      site: "QQ音乐",
      source_url: "https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo",
      duration_s: 10,
    })
    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })
    expect(result.current.preview).not.toBe("")

    // 第二次失败：界面上不能还留着第一次的音频，否则用户会以为那是这次拉的
    fetchYtdlp.mockRejectedValueOnce(new Error("QQ音乐 这首歌要给登录态"))
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    expect(result.current.preview).toBe("")
    expect(result.current.fetchedSourceUrl).toBe("")
    expect(result.current.errorMessage).toContain("登录态")
  })
})
