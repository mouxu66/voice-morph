/**
 * 「在线扒歌」页的取回态版式。
 *
 * 守的是**改动前不存在的两个出口**：
 *
 * 1. **取消按钮**：取回是同步阻塞的，没有这条出口用户唯一能做的是关页面 ——
 *    而后台 yt-dlp 还在跑（改动前就是这样）。
 * 2. **进度**：只写"取回中…"的话，用户无法区分"在慢慢下"和"早就卡住了"。
 *
 * 还有一条**反面**：进度未知（`percent: null`）时不许画进度条、不许显示 0%。
 * "还不知道"和"一点没下"是两回事，把它渲染成 0% 是在编数据 ——
 * 用户会据此以为传输卡在了开头。
 *
 * 页面组件只吃一个 `useYtdlp` 的返回值，所以这里直接给一份手写的 props，
 * 不拉真 hook（也就不会碰网络与后端）。
 */
import { render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { YtdlpPage } from "./YtdlpPage"

type Props = Parameters<typeof YtdlpPage>[0]

function props(over: Partial<Props> = {}): Props {
  return {
    status: {
      available: true,
      path: "C:\\x\\yt-dlp.exe",
      version: "2026.03.17",
      sites: [{ name: "网易云音乐", example: "music.163.com" }],
      cookie: { mode: "none", detail: "", ok: true },
      hint: "",
    },
    probing: false,
    reprobe: vi.fn(),
    url: "https://music.163.com/song?id=1",
    setUrl: vi.fn(),
    fetching: false,
    fetchFromUrl: vi.fn().mockResolvedValue(undefined),
    cancel: vi.fn().mockResolvedValue(undefined),
    progress: { stage: "", percent: null },
    errorMessage: "",
    feedback: "",
    preview: "",
    summary: "",
    fetchedName: "",
    fetchedSite: "",
    fetchedSourceUrl: "",
    submittedUrl: "",
    clear: vi.fn(),
    ready: true,
    ...over,
  } as Props
}

describe("YtdlpPage · 取回态", () => {
  it("闲置时只有「取回并试听」，不出现取消", () => {
    render(<YtdlpPage {...props()} />)
    expect(screen.getByRole("button", { name: /取回并试听/ })).toBeTruthy()
    expect(screen.queryByRole("button", { name: /取消/ })).toBeNull()
  })

  it("★ 取回中给出「取消」出口 —— 否则用户唯一能做的是关页面", () => {
    render(<YtdlpPage {...props({ fetching: true, progress: { stage: "下载中", percent: 42.5 } })} />)
    expect(screen.getByRole("button", { name: /取消/ })).toBeTruthy()
  })

  it("★ 按钮上带阶段与百分比，而不是一句干等", () => {
    render(<YtdlpPage {...props({ fetching: true, progress: { stage: "下载中", percent: 42.5 } })} />)
    expect(screen.getByRole("button", { name: /下载中 42%/ })).toBeTruthy()
  })

  it("★ 进度未知时不许显示 0%（那是「一点没下」的意思）", () => {
    render(<YtdlpPage {...props({ fetching: true, progress: { stage: "", percent: null } })} />)
    expect(screen.getByRole("button", { name: /取回中…/ })).toBeTruthy()
    expect(screen.queryByText(/^0%$/)).toBeNull()
  })
})
