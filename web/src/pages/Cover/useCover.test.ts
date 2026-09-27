import { describe, expect, it, vi, beforeEach } from "vitest"
import { act, fireEvent, render, renderHook, waitFor } from "@testing-library/react"

import { useCover } from "@/pages/Cover/useCover"
import { CoverPage } from "@/pages/Cover/CoverPage"

/**
 * `useCover` / `CoverPage` 的**状态机易错点**守卫（2026-09-27 修的三处）。
 *
 * 这三处的共同点是"都不报错，只是用户被误导"：
 *   1. `start()` 提交失败时，上轮「翻唱完成」面板原地不动 —— 不打个"上一次"标，
 *      新的「翻唱失败」错误条就和旧成品同屏，用户分不清错误说的是哪一次；
 *   2. 粘直链下载成功会静默清掉已选的本地文件（来源只能有一个）—— 不说出来，
 *      用户以为"我刚选的那首怎么没了"；
 *   3. 下载进行中不禁选文件 —— 此刻点的文件会在下载完成时被静默清掉，白点。
 *
 * 同项目的铁律：mock 只替掉 `@/api/client` 的网络函数，其余走真实实现
 * （friendlyError / mediaUrl 的逻辑也在 coverage 内）。
 */

const IDLE_STATUS = {
  running: false,
  status: "idle" as const,
  step: "",
  message: "",
  percent: 0,
  voice_id: "",
  url: "",
  duration_s: 0,
  pitch: 0,
  vocal_gain_applied: 0,
  error: "",
}

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client")
  return {
    ...actual,
    listRvcVoices: vi.fn(),
    getCoverStatus: vi.fn(),
    runCover: vi.fn(),
    fetchCoverUrl: vi.fn(),
    suggestCoverPitch: vi.fn(),
    mediaUrl: (u: string) => u,
  }
})

const client = await import("@/api/client")
const listRvcVoices = vi.mocked(client.listRvcVoices)
const getCoverStatus = vi.mocked(client.getCoverStatus)
const runCover = vi.mocked(client.runCover)
const fetchCoverUrl = vi.mocked(client.fetchCoverUrl)

beforeEach(() => {
  listRvcVoices.mockResolvedValue({
    voices: [{ id: "kangaroo", display_name: "袋鼠", model_ready: true }],
  } as never)
  getCoverStatus.mockResolvedValue(IDLE_STATUS)
})

/** 等挂载 effect（音色清单）跑完再动手，避免断言打在初始态上 */
async function mounted() {
  const h = renderHook(() => useCover())
  await waitFor(() => expect(h.result.current.voices.length).toBe(1))
  return h
}

describe("useCover · 提交失败不混淆", () => {
  it("★ start() 失败 → 旧成品打 resultStale，且来源不动（可直接重试）", async () => {
    // 上一轮已经出过成品，结果区挂着 done 面板
    getCoverStatus.mockResolvedValue({
      ...IDLE_STATUS,
      status: "done",
      url: "/media/session/old.wav",
      duration_s: 30,
      pitch: 3,
      vocal_gain_applied: 1.5,
    })
    runCover.mockRejectedValueOnce(new Error("RVC 训练正在运行"))

    const { result } = await mounted()
    expect(result.current.resultStale).toBe(false)

    await act(async () => {
      await result.current.pickFile(new File([new Uint8Array([1])], "song.mp3", { type: "audio/mpeg" }))
    })
    await act(async () => {
      await result.current.start()
    })

    expect(result.current.errorMessage).toBe("RVC 训练正在运行")
    // 面板还是上一轮的 done（没有被乐观 running 态覆盖），但必须被打上"上一次"标
    expect(result.current.status?.status).toBe("done")
    expect(result.current.resultStale).toBe(true)
    // 失败不清来源：用户改个参数就能直接再点「开始翻唱」
    expect(result.current.file).not.toBeNull()
    expect(result.current.running).toBe(false)
  })

  it("失败后再提交成功 → 标记复位（新 done 面板就是这次的成果）", async () => {
    runCover.mockRejectedValueOnce(new Error("RVC 训练正在运行"))
    runCover.mockResolvedValue({ ok: true, voice_id: "kangaroo" })

    const { result } = await mounted()
    await act(async () => {
      await result.current.pickFile(new File([new Uint8Array([1])], "song.mp3"))
    })
    await act(async () => {
      await result.current.start()
    })
    expect(result.current.resultStale).toBe(true)

    await act(async () => {
      await result.current.start()
    })
    expect(result.current.errorMessage).toBe("")
    expect(result.current.resultStale).toBe(false)
    expect(result.current.status?.status).toBe("running")
  })
})

describe("useCover · 粘直链下载", () => {
  it("★ 已选本地文件时下载成功 → 明说来源被切换，且 file 被清掉", async () => {
    fetchCoverUrl.mockResolvedValue({
      ok: true,
      name: "cover_src_2.mp3",
      url: "/media/session/cover_src_2.mp3",
      bytes: 3 * 1024 * 1024,
      duration_s: 61,
    })

    const { result } = await mounted()
    await act(async () => {
      await result.current.pickFile(new File([new Uint8Array([1])], "mine.mp3"))
    })
    act(() => {
      result.current.setSrcUrl("https://example.com/s.mp3")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    // 单来源不变量：本地文件让位给直链
    expect(result.current.file).toBeNull()
    expect(result.current.srcName).toBe("cover_src_2.mp3")
    // 但这件事必须说出来 —— 静默清文件是"来源自己变了"的来源
    expect(result.current.feedback).toContain("来源已切换")
    expect(result.current.errorMessage).toBe("")
  })

  it("没选文件时下载成功 → 反馈不提切换（没有东西被换掉）", async () => {
    fetchCoverUrl.mockResolvedValue({
      ok: true,
      name: "cover_src_3.mp3",
      url: "/media/session/cover_src_3.mp3",
      bytes: 1024,
      duration_s: 0,
    })

    const { result } = await mounted()
    act(() => {
      result.current.setSrcUrl("https://example.com/s.mp3")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    expect(result.current.feedback).toBe("已下好，可以先试听 —— 确认是这首歌再点「开始翻唱」")
  })

  it("下载失败 → 错误进错误区，不留半个预览", async () => {
    fetchCoverUrl.mockRejectedValueOnce(new Error("502 Bad Gateway"))

    const { result } = await mounted()
    act(() => {
      result.current.setSrcUrl("https://example.com/bad.mp3")
    })
    await act(async () => {
      await result.current.fetchFromUrl()
    })

    expect(result.current.errorMessage).toBe("502 Bad Gateway")
    expect(result.current.srcName).toBe("")
    expect(result.current.srcPreview).toBe("")
    expect(result.current.feedback).toBe("")
  })
})

describe("CoverPage · 下载期间禁选文件", () => {
  function fakeProps(over: Partial<ReturnType<typeof useCover>> = {}): ReturnType<typeof useCover> {
    return {
      voices: [],
      voiceId: "",
      setVoiceId: () => {},
      file: null,
      pickFile: () => {},
      srcName: "",
      srcUrl: "",
      setSrcUrl: () => {},
      srcPreview: "",
      srcSummary: "",
      fetching: false,
      fetchFromUrl: async () => {},
      clearSource: () => {},
      pitch: 0,
      setPitch: () => {},
      autoPitch: false,
      setAutoPitch: () => {},
      indexRate: 0.5,
      setIndexRate: () => {},
      vocalGain: 1,
      setVocalGain: () => {},
      accompGain: 1,
      setAccompGain: () => {},
      autoGain: true,
      setAutoGain: () => {},
      submitting: false,
      analyzing: false,
      errorMessage: "",
      feedback: "",
      status: null,
      running: false,
      resultUrl: "",
      resultStale: false,
      analyzePitch: async () => {},
      start: async () => {},
      hasSource: false,
      ...over,
    }
  }

  /** 点拖拽区最终会调用隐藏 file input 的原生 click() —— 用事件监听数它 */
  function zoneAndInput(container: HTMLElement) {
    const zone = container.querySelector('[role="button"]')
    const input = container.querySelector('input[type="file"]')
    if (!zone || !input) throw new Error("找不到拖拽区/文件 input")
    const spy = vi.fn()
    input.addEventListener("click", spy)
    return { zone, spy }
  }

  it("★ fetching 中点拖拽区 → 不触发文件选择（选了也会被下载完成清掉，白点）", () => {
    const { container } = render(<CoverPage {...fakeProps({ fetching: true })} />)
    const { zone, spy } = zoneAndInput(container)

    fireEvent.click(zone)

    expect(spy).not.toHaveBeenCalled()
  })

  it("空闲时点拖拽区 → 正常触发隐藏的文件 input", () => {
    const { container } = render(<CoverPage {...fakeProps()} />)
    const { zone, spy } = zoneAndInput(container)

    fireEvent.click(zone)

    expect(spy).toHaveBeenCalledTimes(1)
  })
})

describe("CoverPage · 完成面板的上一次标记", () => {
  const DONE_STATUS = {
    ...IDLE_STATUS,
    status: "done" as const,
    running: false,
    url: "/media/session/x.wav",
    duration_s: 42,
    pitch: 3,
    vocal_gain_applied: 1.5,
  }

  it("resultStale → 面板带「上一次的结果」徽标与一句解释", () => {
    render(
      <CoverPage
        {...({
          ...fakePropsForPanel(),
          status: DONE_STATUS,
          resultUrl: "/media/session/x.wav",
          resultStale: true,
        } as ReturnType<typeof useCover>)}
      />,
    )

    expect(screenText("上一次的结果")).toBeInTheDocument()
    expect(screenText("刚才的尝试没有成功，下面是上一次的成品。")).toBeInTheDocument()
  })

  it("非 stale → 没有徽标（刚跑出来的成品不需要解释）", () => {
    render(
      <CoverPage
        {...({
          ...fakePropsForPanel(),
          status: DONE_STATUS,
          resultUrl: "/media/session/x.wav",
          resultStale: false,
        } as ReturnType<typeof useCover>)}
      />,
    )

    expect(screen.queryByText("上一次的结果")).not.toBeInTheDocument()
  })
})

function fakePropsForPanel(): ReturnType<typeof useCover> {
  return {
    voices: [{ id: "kangaroo", display_name: "袋鼠", model_ready: true }],
    voiceId: "kangaroo",
    setVoiceId: () => {},
    file: null,
    pickFile: () => {},
    srcName: "",
    srcUrl: "",
    setSrcUrl: () => {},
    srcPreview: "",
    srcSummary: "",
    fetching: false,
    fetchFromUrl: async () => {},
    clearSource: () => {},
    pitch: 3,
    setPitch: () => {},
    autoPitch: false,
    setAutoPitch: () => {},
    indexRate: 0.5,
    setIndexRate: () => {},
    vocalGain: 1,
    setVocalGain: () => {},
    accompGain: 1,
    setAccompGain: () => {},
    autoGain: true,
    setAutoGain: () => {},
    submitting: false,
    analyzing: false,
    errorMessage: "",
    feedback: "",
    status: null,
    running: false,
    resultUrl: "",
    resultStale: false,
    analyzePitch: async () => {},
    start: async () => {},
    hasSource: false,
  }
}
