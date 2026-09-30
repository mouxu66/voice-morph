import { describe, expect, it, vi, beforeEach } from "vitest"
import { renderHook, waitFor, act } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import type { ReactNode } from "react"

import { useOfflineVc } from "@/pages/OfflineVc/useOfflineVc"
import { offlineVcDeepLink, liveDeepLink } from "@/lib/deepLink"
import type { OfflineVcStatus, RvcVoice } from "@/api/client"

/**
 * 深链预选音色（`?voice=`）的**诚实性**守卫 —— 首页「录一句听听」带过来的那条路。
 *
 * 这条路的价值在于"降低第一次成功的门槛"：只要能录一句、几秒听到结果，
 * 用户就相信"变声真的有效"。而它最容易出的问题**不是报错，是误导**：
 *
 *   1. 音色其实没就绪，却显示"已为你选好"→ 用户对着一个不是自己挑的音色录完，
 *      白录一遍（本页最贵的浪费：录音是用户亲手做的事）；
 *   2. 后端没起来（清单读不到）也报"没找到音色"→ 一句假警报，把"服务没起"
 *      说成了"你的音色坏了"，用户会去重装一个本来好好的音色。
 *
 * 这两条都靠 `voicesLoaded`（读到了没有）与 `rvcVoices`（里面有没有）**分开判断**
 * 来保证。测试就是把这两条钉死。
 */

const KANGAROO: RvcVoice = {
  id: "kangaroo",
  display_name: "袋鼠骑士",
  has_reference: true,
  pth_exists: true,
  index_exists: true,
  model_ready: true,
  dataset_count: 73,
  trained_at: "2026-09-01 10:00:00",
}

const OTHER: RvcVoice = {
  id: "merg_004",
  display_name: "另一个音色",
  has_reference: true,
  pth_exists: true,
  index_exists: true,
  model_ready: true,
  dataset_count: 0,
  trained_at: "",
}

const IDLE: OfflineVcStatus = {
  running: false,
  status: "idle",
  message: "",
  voice_id: "",
  url: "",
  duration_s: 0,
  error: "",
}

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client")
  return {
    ...actual,
    listRvcVoices: vi.fn(),
    getOfflineVcStatus: vi.fn(),
    runOfflineVc: vi.fn(),
    suggestPitch: vi.fn(),
    mediaUrl: (u: string) => u,
  }
})

const client = await import("@/api/client")
const listRvcVoices = vi.mocked(client.listRvcVoices)
const getOfflineVcStatus = vi.mocked(client.getOfflineVcStatus)

function wrapperFor(path: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
  )
}

function voicesOf(...vs: RvcVoice[]) {
  return { voices: vs, active_exp: vs[0]?.id ?? "", default_exp: "", rvc_root: "", rvc_ready: true }
}

beforeEach(() => {
  vi.clearAllMocks()
  getOfflineVcStatus.mockResolvedValue(IDLE)
})

describe("深链预选音色（?voice=）", () => {
  it("清单里有这个音色 → 选中它，并亮出引导条", async () => {
    listRvcVoices.mockResolvedValue(voicesOf(KANGAROO, OTHER))
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc?voice=kangaroo"),
    })

    await waitFor(() => expect(result.current.voiceId).toBe("kangaroo"))
    expect(result.current.deepLinkGuide).toBe(true)
    expect(result.current.deepLinkMiss).toBe(false)
    expect(result.current.deepLinkVoiceId).toBe("kangaroo")
  })

  it("★ 清单里没有这个音色 → 落回默认，并**如实说没找到**（不假装选中）", async () => {
    listRvcVoices.mockResolvedValue(voicesOf(OTHER))
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc?voice=ghost"),
    })

    await waitFor(() => expect(result.current.deepLinkMiss).toBe(true))
    expect(result.current.deepLinkGuide).toBe(false) // 绝不能同时说"已为你选好"
    expect(result.current.voiceId).toBe("merg_004") // 落回清单里的第一个，不是 ghost
  })

  it("★ 读不到清单（后端没起）→ 不报「没找到」，那是假警报", async () => {
    listRvcVoices.mockRejectedValue(new Error("backend down"))
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc?voice=kangaroo"),
    })

    // 等服务请求失败被吞掉、状态稳定下来
    await waitFor(() => expect(listRvcVoices).toHaveBeenCalled())
    await act(async () => {
      await Promise.resolve()
    })

    expect(result.current.deepLinkMiss).toBe(false)
    expect(result.current.deepLinkGuide).toBe(false)
    // 音色清单读不到 ≠ 音色不存在：这时页面自己会显示离线提示，不该再补一句假警报
  })

  it("★ 清单读到了、但里面一个音色都没有 → 仍要如实说没找到（不是静默失败）", async () => {
    // 这条与上一条是**成对**的：两者都得到空清单，但成因完全不同 ——
    // 一个是"没问到"，一个是"问到了、确实没有"。只有分得清，才不会该报的时候不报。
    // （用 `rvcVoices.length` 代替 `voicesLoaded` 做判断时，这条会红 —— 判据的自检用例。）
    listRvcVoices.mockResolvedValue(voicesOf())
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc?voice=kangaroo"),
    })

    await waitFor(() => expect(result.current.deepLinkMiss).toBe(true))
    expect(result.current.deepLinkGuide).toBe(false)
    expect(result.current.voiceId).toBe("") // 一个能用的都没有，就该空着
  })

  it("没有 ?voice= 参数 → 一切照旧，不亮引导条", async () => {
    listRvcVoices.mockResolvedValue(voicesOf(KANGAROO))
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc"),
    })

    await waitFor(() => expect(result.current.voiceId).toBe("kangaroo"))
    expect(result.current.deepLinkGuide).toBe(false)
    expect(result.current.deepLinkMiss).toBe(false)
  })

  it("只预选一次：用户随后手改音色，不会被拽回去", async () => {
    listRvcVoices.mockResolvedValue(voicesOf(KANGAROO, OTHER))
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc?voice=kangaroo"),
    })
    await waitFor(() => expect(result.current.voiceId).toBe("kangaroo"))

    act(() => result.current.setVoiceId("merg_004"))
    act(() => result.current.setPitch(5)) // 再触发一次重渲染

    expect(result.current.voiceId).toBe("merg_004")
  })

  it("引导条可以关掉", async () => {
    listRvcVoices.mockResolvedValue(voicesOf(KANGAROO))
    const { result } = renderHook(() => useOfflineVc(), {
      wrapper: wrapperFor("/offlinevc?voice=kangaroo"),
    })
    await waitFor(() => expect(result.current.deepLinkGuide).toBe(true))

    act(() => result.current.dismissDeepLinkGuide())
    expect(result.current.deepLinkGuide).toBe(false)
  })
})

describe("深链地址（首页两个动作各自要去哪）", () => {
  /**
   * 这两个地址**本身就是产品决策**，值得单独钉住：
   *  「录一句听听」必须去**离线变声**（几秒出结果），
   *  「开麦」才去实时变声（要先配虚拟声卡）。
   * 改错一个字符不会报任何错，只会让"点了之后要做的事"悄悄变难 —— 最难被发现的那种回归。
   */
  it("「录一句听听」→ 离线变声，且音色 id 带过去", () => {
    expect(offlineVcDeepLink("kangaroo")).toBe("/offlinevc?voice=kangaroo")
  })

  it("「开麦」→ 实时变声的 rvc 标签页", () => {
    expect(liveDeepLink("kangaroo")).toBe("/live?tab=rvc&voice=kangaroo")
  })

  it("音色 id 里的特殊字符要转义（否则 URL 会断）", () => {
    expect(offlineVcDeepLink("a b&c")).toBe("/offlinevc?voice=a%20b%26c")
  })
})
