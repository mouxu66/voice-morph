import { describe, expect, it, vi, beforeEach } from "vitest"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"

import { BgmPanel } from "@/pages/Tts/BgmPanel"
import type * as clientModule from "@/api/client"

/**
 * BgmPanel（歌声垫底 A/C 两档）的行为守卫：
 *   1. A 档：选歌 → 「垫进这条语音」把 onPremixed 交回调用方（发送目标换成 bgmmix_*）；
 *   2. A 档没垫底源 → 按钮置灰且**说明缺什么**（"先合成一条语音"），不是静默死按钮；
 *   3. C 档：开始/停止走 /bgm/start、/bgm/stop，音量滑块在垫歌进行中实时调 /bgm/gain。
 *
 * 铁律：mock 只替掉 `@/api/client` 的网络函数；`usePluginCatalog` 走真实现
 * （测试环境清单拿不到 → catalog=null → `pluginVisible` 放行，门控语义本身由
 * `crossPluginGate.test.ts` 的源码扫描与 `pluginRoutes.test.ts` 覆盖）。
 */

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof clientModule>("@/api/client")
  return {
    ...actual,
    bgmUpload: vi.fn(),
    bgmPremix: vi.fn(),
    bgmStart: vi.fn(),
    bgmStop: vi.fn(),
    bgmGain: vi.fn(),
  }
})

const client = await import("@/api/client")
const bgmUpload = vi.mocked(client.bgmUpload)
const bgmPremix = vi.mocked(client.bgmPremix)
const bgmStart = vi.mocked(client.bgmStart)
const bgmStop = vi.mocked(client.bgmStop)
const bgmGain = vi.mocked(client.bgmGain)

beforeEach(() => {
  vi.clearAllMocks() // 用例间互不看见对方的调用次数（"没选歌不该发命令"这类断言依赖它）
  bgmUpload.mockResolvedValue({ ok: true, name: "bgm_t1.wav", duration_s: 200, url: "/api/media/outputs/session/bgm_t1.wav" })
  bgmPremix.mockResolvedValue({
    ok: true,
    wav: "bgmmix_tts_x.wav",
    seconds: 8.2,
    url: "/api/media/outputs/session/bgmmix_tts_x.wav",
    notes: [],
  })
  bgmStart.mockResolvedValue({ ok: true, playing: true, id: "bgm_t1", duration_s: 200, loop: true })
  bgmStop.mockResolvedValue({ ok: true })
  bgmGain.mockResolvedValue({ ok: true, gain: 0.5 })
})

/** 选一首歌（走隐藏 input 的 change 事件，等上传完成、歌名回显） */
async function pickSong() {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  expect(input).toBeTruthy()
  fireEvent.change(input, { target: { files: [new File([new Uint8Array([1])], "song.mp3", { type: "audio/mpeg" })] } })
  await waitFor(() => expect(screen.getByText(/bgm_t1\.wav/)).toBeInTheDocument())
}

describe("BgmPanel · A 档 歌声垫底预混", () => {
  it("★ 选歌 → 垫进这条语音 → onPremixed 拿到 bgmmix 产物", async () => {
    const onPremixed = vi.fn()
    render(<BgmPanel mode="premix" wav="tts_x.wav" sourceSeconds={8.2} onPremixed={onPremixed} />)
    await pickSong()

    fireEvent.click(screen.getByRole("button", { name: /垫进这条语音/ }))
    await waitFor(() => expect(onPremixed).toHaveBeenCalled())

    expect(bgmPremix).toHaveBeenCalledWith({ wav: "tts_x.wav", bgm: "bgm_t1.wav", lead_db: 10, start_s: 0 })
    expect(onPremixed).toHaveBeenCalledWith(
      expect.objectContaining({ wav: "bgmmix_tts_x.wav", seconds: 8.2 }),
    )
  })

  it("没有垫底源 → 按钮置灰并说明缺什么（先合成一条语音）", () => {
    render(<BgmPanel mode="premix" />)
    const btn = screen.getByRole("button", { name: /垫进这条语音/ })
    expect(btn).toBeDisabled()
    expect(btn.title).toContain("先在上面合成一条语音")
  })

  it("上传失败 → 错误显示出来，不留下半截状态", async () => {
    bgmUpload.mockRejectedValueOnce(new Error("这首歌读不出来"))
    render(<BgmPanel mode="premix" wav="tts_x.wav" />)
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File([new Uint8Array([1])], "bad.mp3")] } })

    await waitFor(() => expect(screen.getByText("这首歌读不出来")).toBeInTheDocument())
    expect(screen.queryByText(/bgm_t1\.wav/)).not.toBeInTheDocument()
  })
})

describe("BgmPanel · C 档 实时垫歌", () => {
  it("★ 开始 → /bgm/start（默认 gain 0.3 + 循环）；停止 → /bgm/stop", async () => {
    render(<BgmPanel mode="layer" />)
    await pickSong()

    fireEvent.click(screen.getByRole("button", { name: /开始垫歌/ }))
    await waitFor(() => expect(bgmStart).toHaveBeenCalledWith("bgm_t1.wav", 0.3, true))

    fireEvent.click(screen.getByRole("button", { name: /停止垫歌/ }))
    await waitFor(() => expect(bgmStop).toHaveBeenCalledTimes(1))
  })

  it("垫歌进行中拖音量 → 实时调 /bgm/gain（不用重开）", async () => {
    render(<BgmPanel mode="layer" />)
    await pickSong()

    fireEvent.click(screen.getByRole("button", { name: /开始垫歌/ }))
    await waitFor(() => expect(screen.getByText(/垫歌进行中/)).toBeInTheDocument())

    const slider = screen.getByRole("slider", { name: "垫底歌的音量" }) as HTMLInputElement
    fireEvent.change(slider, { target: { value: "0.5" } })
    await waitFor(() => expect(bgmGain).toHaveBeenCalledWith(0.5))
  })

  it("还没选歌 → 开始按钮置灰（没有任何命令发出去）", () => {
    render(<BgmPanel mode="layer" />)
    expect(screen.getByRole("button", { name: /开始垫歌/ })).toBeDisabled()
    expect(bgmStart).not.toHaveBeenCalled()
  })
})
