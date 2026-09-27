import { beforeEach, describe, expect, it, vi } from "vitest"
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
  const actual = await vi.importActual<Record<string, unknown>>("@/api/client")
  return {
    ...actual,
    ytdlpStatus: vi.fn(),
    fetchYtdlp: vi.fn(),
    ytdlpJob: vi.fn(),
    ytdlpCancel: vi.fn(),
    mediaUrl: (u: string) => u,
  }
})

const client = await import("@/api/client")
const ytdlpStatus = vi.mocked(client.ytdlpStatus)
const fetchYtdlp = vi.mocked(client.fetchYtdlp)
const ytdlpJob = vi.mocked(client.ytdlpJob)
const ytdlpCancel = vi.mocked(client.ytdlpCancel)

const OK_RESULT = {
  ok: true,
  name: "ytdlp_1.mp3",
  url: "/media/session/ytdlp_1.mp3",
  bytes: 1024,
  site: "QQ音乐",
  source_url: "https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo",
  duration_s: 152,
}

/**
 * 清掉调用记录。**不加这一句会读出上一条用例的调用** ——
 * `fetchYtdlp.mock.calls[0]` 拿到的是别人提交的作业号，断言就会以
 * "两个 uuid 不一样"的形式红，看着像业务 bug，其实是测试串味。
 * （用 `clearAllMocks` 而不是 `resetAllMocks`：前者只清记录，不清实现。）
 */
beforeEach(() => {
  vi.clearAllMocks()
})

/** 让 `fetchYtdlp` 一直挂着，直到测试自己来结束它。 */
function pendingFetch() {
  let release!: (v: typeof OK_RESULT) => void
  let fail!: (e: unknown) => void
  fetchYtdlp.mockImplementation(
    () => new Promise((res, rej) => {
      release = res
      fail = rej
    }),
  )
  return { release: () => release(OK_RESULT), fail: (e: unknown) => fail(e) }
}

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

/**
 * 会员曲兜底（MV 抽音轨）在前端的样子。
 *
 * 后端默认关掉它；开着时，yt-dlp 拿不到音频（会员曲）会改从官方 MV 抽音轨，
 * 返回 `via: "mv_fallback"` + `quality_note`。前端要做的只有一件事：
 * **把"这份音频是打折的"这件事说清楚** —— 不能让兜底产物在界面上长得
 * 和正版音源一样，否则用户会以为这首歌在平台上本来就是这个音质。
 */
describe("useYtdlp · 会员曲兜底（MV 抽轨）", () => {
  it("★ via=mv_fallback 时要把音质说明透出来", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    fetchYtdlp.mockResolvedValue({
      ok: true,
      name: "ytdlp_mv_1.m4a",
      url: "/media/session/ytdlp_mv_1.m4a",
      bytes: 6561304,
      site: "QQ音乐（MV 抽轨）",
      source_url: "https://y.qq.com/n/ryqq/mv/r0035thc5pb",
      duration_s: 271,
      via: "mv_fallback",
      quality_note: "音质：MV 抽轨（27 片合并，约 192kbps AAC），非正版母带，且混有影像声音。",
    })

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://c6.y.qq.com/base/fcgi-bin/u?__=yY3vbmLH9kYO")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    // 页面靠这个非空来决定渲不渲染那条琥珀色告警
    expect(result.current.qualityNote).toContain("MV 抽轨")
    expect(result.current.qualityNote).toContain("非正版母带")
    // 提示语也要和正版路径**分开**，别让用户以为一切正常
    expect(result.current.feedback).toContain("会员")
    expect(result.current.feedback).toContain("折损")
  })

  it("★ 正版音源不该被标成打折（via 缺省时 qualityNote 保持空）", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    fetchYtdlp.mockResolvedValue(OK_RESULT)

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://music.163.com/song?id=1")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    expect(result.current.qualityNote).toBe("")
    expect(result.current.feedback).not.toContain("折损")
  })

  it("★ 兜底产物的音质说明不能在失败/清空时残留", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    fetchYtdlp.mockResolvedValueOnce({
      ok: true,
      name: "ytdlp_mv_1.m4a",
      url: "/media/session/ytdlp_mv_1.m4a",
      bytes: 100,
      site: "QQ音乐（MV 抽轨）",
      source_url: "https://y.qq.com/n/ryqq/mv/r0035thc5pb",
      duration_s: 271,
      via: "mv_fallback",
      quality_note: "音质：MV 抽轨",
    })

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://c6.y.qq.com/base/fcgi-bin/u?__=yY3vbmLH9kYO")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })
    expect(result.current.qualityNote).not.toBe("")

    // 下一次拿的是正版音源：上一轮的告警必须消失，否则会误导成"这次也是打折的"
    fetchYtdlp.mockResolvedValueOnce(OK_RESULT)
    await act(async () => {
      await result.current.fetchFromUrl()
    })
    expect(result.current.qualityNote).toBe("")

    // 手动「丢弃」也要清掉
    fetchYtdlp.mockResolvedValueOnce({
      ok: true,
      name: "m.m4a",
      url: "/media/session/m.m4a",
      bytes: 100,
      site: "QQ音乐（MV 抽轨）",
      source_url: "https://y.qq.com/n/ryqq/mv/r0035thc5pb",
      duration_s: 10,
      via: "mv_fallback",
      quality_note: "音质：MV 抽轨",
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })
    expect(result.current.qualityNote).not.toBe("")
    await act(async () => {
      result.current.clear()
    })
    expect(result.current.qualityNote).toBe("")
  })
})

/**
 * 取回必须能停 + 有进度。
 *
 * 改之前：`fetchFromUrl` 没有 AbortSignal、后端也没有取消端点，粘一条坏链接
 * UI 会卡在"取回中…"最长十分钟，用户唯一能做的是关页面 —— 而后台 yt-dlp
 * 还在跑。这几条钉住"能停"和"停的时候用户看到什么"。
 */
describe("useYtdlp · 取回可取消 + 进度", () => {
  it("★ 取回时会带上作业号，并把请求的 signal 交出去", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    const { release } = pendingFetch()

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://music.163.com/song?id=1")
    })
    let flying!: Promise<void>
    await act(async () => {
      flying = result.current.fetchFromUrl()
    })

    // 作业号要合规（后端只认 ^[0-9A-Za-z_-]{8,64}$）—— 否则取回会被 400 挡掉
    const [url, jobId, signal] = fetchYtdlp.mock.calls[0]
    expect(url).toBe("https://music.163.com/song?id=1")
    expect(jobId).toMatch(/^[0-9A-Za-z_-]{8,64}$/)
    // signal 拿不到就没法本地 abort —— 取消只能等后端，慢一拍
    expect(signal).toBeInstanceOf(AbortSignal)

    await act(async () => {
      release()
      await flying
    })
  })

  it("★ 取消要先叫后端停，再 abort 本地请求", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    const { fail } = pendingFetch()
    ytdlpCancel.mockResolvedValue({ ok: true, found: true })

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://music.163.com/song?id=1")
    })
    let flying!: Promise<void>
    await act(async () => {
      flying = result.current.fetchFromUrl()
    })
    const jobId = fetchYtdlp.mock.calls[0][1]

    await act(async () => {
      await result.current.cancel()
    })
    expect(ytdlpCancel).toHaveBeenCalledWith(jobId)
    // 顺序：后端先收到取消，本地再断 —— 反了的话取消请求可能根本没发出去
    expect(ytdlpCancel.mock.invocationCallOrder[0]).toBeLessThan(
      ytdlpJob.mock.invocationCallOrder[0] ?? Number.MAX_SAFE_INTEGER,
    )

    // abort 之后 jsonFetch 会以 AbortError 收场
    fail(Object.assign(new Error("aborted"), { name: "AbortError" }))
    await act(async () => {
      await flying
    })

    // 用户自己按的取消**不是失败**：不该弹红条
    expect(result.current.errorMessage).toBe("")
    expect(result.current.feedback).toContain("已取消")
    expect(result.current.fetching).toBe(false)
  })

  it("★ 取消接口挂了也照样停（本地 abort + 后端断连兜底还在）", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    const { fail } = pendingFetch()
    ytdlpCancel.mockRejectedValue(new Error("后端没响应"))

    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://music.163.com/song?id=1")
    })
    let flying!: Promise<void>
    await act(async () => {
      flying = result.current.fetchFromUrl()
    })

    await act(async () => {
      await result.current.cancel()
    })
    fail(Object.assign(new Error("aborted"), { name: "AbortError" }))
    await act(async () => {
      await flying
    })

    expect(result.current.fetching).toBe(false)
    expect(result.current.errorMessage).toBe("") // 取消失败不该被渲染成拉取失败
  })

  it("进度从后端取回来，且 percent 缺失时保持 null（不能显示成 0%）", async () => {
    vi.useFakeTimers()
    try {
      ytdlpStatus.mockResolvedValue(READY)
      const { release } = pendingFetch()
      ytdlpJob.mockResolvedValue({
        found: true,
        stage: "下载中",
        percent: 42.5,
        cancelled: false,
        elapsed_s: 3.2,
      })

      const { result } = renderHook(() => useYtdlp())
      await act(async () => {
        await Promise.resolve()
      })
      await act(async () => {
        result.current.setUrl("https://music.163.com/song?id=1")
      })
      let flying!: Promise<void>
      await act(async () => {
        flying = result.current.fetchFromUrl()
      })
      expect(result.current.progress.percent).toBeNull()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100)
      })
      expect(result.current.progress.stage).toBe("下载中")
      expect(result.current.progress.percent).toBe(42.5)
      expect(ytdlpJob).toHaveBeenCalledWith(fetchYtdlp.mock.calls[0][1])

      await act(async () => {
        release()
        await flying
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it("进度轮询失败不打扰用户（它不是失败，只是查不到）", async () => {
    vi.useFakeTimers()
    try {
      ytdlpStatus.mockResolvedValue(READY)
      const { release } = pendingFetch()
      ytdlpJob.mockRejectedValue(new Error("boom"))

      const { result } = renderHook(() => useYtdlp())
      await act(async () => {
        await Promise.resolve()
      })
      await act(async () => {
        result.current.setUrl("https://music.163.com/song?id=1")
      })
      let flying!: Promise<void>
      await act(async () => {
        flying = result.current.fetchFromUrl()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100)
      })

      expect(result.current.errorMessage).toBe("")

      await act(async () => {
        release()
        await flying
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it("取回结束后进度状态清空，不残留上一轮的数字", async () => {
    ytdlpStatus.mockResolvedValue(READY)
    fetchYtdlp.mockResolvedValue(OK_RESULT)
    const { result } = renderHook(() => useYtdlp())
    await waitFor(() => expect(result.current.probing).toBe(false))
    await act(async () => {
      result.current.setUrl("https://music.163.com/song?id=1")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })
    expect(result.current.progress).toEqual({ stage: "", percent: null })
    expect(result.current.fetching).toBe(false)
  })
})
