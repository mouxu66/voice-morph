/**
 * 预混时间轴（波形 + 可拖记号）—— 守两件事：
 *
 * 1. **换算的数学**（纯函数直测）：桶取样、秒数换算、量化夹紧、刻度分档。
 *    这些是"拖到哪儿算第几秒"的全部依据，出错的表现是**悄悄偏了半秒**，肉眼看不出来。
 * 2. **失败时不清空能力**：波形取不到（没有 Web Audio / fetch 失败）时必须如实写出原因，
 *    而且**秒数框照旧可用** —— 拖不动不等于定不了位。这条是刻意钉的：
 *    "降级成一句可读的说明"与"静默什么都不显示"在界面上看起来一样，但后者会让用户
 *    以为这功能坏了。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"
import { PremixTimeline, peaksFromChannel, secondsAtX, snapSeconds, tickSeconds } from "./PremixTimeline"

describe("取样：采样 → 柱高", () => {
  it("每桶取窗口内峰值，并归一化到 0~1", () => {
    // 4 个样本 → 2 桶：前两格 0.2/0.8（峰值 0.8）、后两格 0.1/0.4（峰值 0.4）
    const peaks = peaksFromChannel(new Float32Array([0.2, 0.8, 0.1, 0.4]), 2)
    expect(peaks).toHaveLength(2)
    expect(peaks[0]).toBeCloseTo(1, 5) // 全局峰值那桶 = 1
    expect(peaks[1]).toBeCloseTo(0.5, 5)
  })

  it("负样本按绝对值取峰值（人声是双极性的）", () => {
    const peaks = peaksFromChannel(new Float32Array([-0.9, 0.1]), 1)
    expect(peaks[0]).toBeCloseTo(1, 5)
  })

  it("空音频不炸，也不返回 NaN（取样数给多少就给多少根柱）", () => {
    const peaks = peaksFromChannel(new Float32Array(0), 5)
    expect(peaks).toHaveLength(5)
    expect(peaks.every((p) => p === 0)).toBe(true)
  })

  it("柱数多于样本数时每根柱仍拿到样本（不产生空洞）", () => {
    const peaks = peaksFromChannel(new Float32Array([0.5, 1]), 8)
    expect(peaks).toHaveLength(8)
    expect(Math.max(...peaks)).toBeCloseTo(1, 5)
  })
})

describe("换算：指针 x → 第几秒", () => {
  it("正中间就是时长的一半", () => {
    expect(secondsAtX(150, 100, 100, 4)).toBe(2)
  })

  it("拖出左右边界被夹在 [0, 时长] 内", () => {
    expect(secondsAtX(0, 100, 100, 4)).toBe(0)
    expect(secondsAtX(99999, 100, 100, 4)).toBe(4)
  })

  it("量化到 0.1s（拖出来的值不该是 3.0000000000000004）", () => {
    const v = secondsAtX(100 + 33.33, 100, 100, 4)
    expect(Number.isInteger(v * 10)).toBe(true)
    expect(v).toBeCloseTo(1.3, 1)
  })

  it("宽度或时长为 0 时返回 0，不返回 NaN —— 没量到尺寸就绝不能动用户的位置", () => {
    expect(secondsAtX(50, 0, 0, 4)).toBe(0)
    expect(secondsAtX(50, 0, 100, 0)).toBe(0)
    expect(Number.isNaN(snapSeconds(Number.NaN, 4))).toBe(false)
  })
})

describe("刻度", () => {
  it("短音频细、长音频粗，且最多 8 条（挤成一片等于没刻度）", () => {
    expect(tickSeconds(4)).toEqual([1, 2, 3])
    expect(tickSeconds(60).length).toBeLessThanOrEqual(8)
    expect(tickSeconds(0)).toEqual([])
  })
})

// ---------------------------------------------------------------- 渲染与拖动

/** 装一个假的 Web Audio（jsdom 没有 decodeAudioData）。 */
function stubAudioContext(duration = 4, samples = 4000) {
  const channel = new Float32Array(samples)
  for (let i = 0; i < samples; i += 1) channel[i] = Math.sin(i / 10) * 0.8
  class FakeCtx {
    async decodeAudioData() {
      return { duration, getChannelData: () => channel }
    }
    async close() {}
  }
  ;(window as unknown as { AudioContext: unknown }).AudioContext = FakeCtx
}

/** jsdom 的 getBoundingClientRect 全是 0 → 拖拽换算拿不到宽度，这里给个真实矩形。 */
function stubRect(el: Element, left: number, width: number) {
  el.getBoundingClientRect = () =>
    ({ left, top: 0, width, height: 64, right: left + width, bottom: 64, x: left, y: 0, toJSON: () => ({}) }) as DOMRect
}

/** jsdom 没有 PointerEvent 构造器：用 MouseEvent 带上 pointerId —— 处理器只读这几个字段。 */
function pointerEvent(type: string, clientX: number) {
  const e = new MouseEvent(type, { bubbles: true, cancelable: true, clientX })
  Object.assign(e, { pointerId: 1 })
  return e
}

afterEach(() => {
  vi.restoreAllMocks()
  delete (window as unknown as { AudioContext?: unknown }).AudioContext
})

describe("波形上的记号", () => {
  it("没有记号时整块不渲染（也不去拉音频）", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch")
    const { container } = render(<PremixTimeline src="/api/media/outputs/tts_a.wav" markers={[]} onMove={() => {}} />)
    expect(container).toBeEmptyDOMElement()
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it("拖动记号 → 按波形宽度换算出新秒数", async () => {
    stubAudioContext(4)
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      arrayBuffer: async () => new ArrayBuffer(16),
    } as unknown as Response)
    const onMove = vi.fn()
    render(
      <PremixTimeline
        src="/api/media/outputs/tts_a.wav"
        seconds={4}
        markers={[{ id: "boom", label: "💥", at_s: 0 }]}
        onMove={onMove}
      />,
    )
    const marker = await screen.findByRole("slider")
    const track = screen.getByTestId("premix-timeline-track")
    stubRect(track, 100, 200) // 200px 宽 = 4s → 中间 = 2s

    fireEvent(marker, pointerEvent("pointerdown", 100))
    fireEvent(marker, pointerEvent("pointermove", 200))
    fireEvent(marker, pointerEvent("pointerup", 200))

    expect(onMove).toHaveBeenCalledWith("boom", 2)
  })

  it("箭头键 ±0.1s（拖不到的那半个像素由键盘补）", async () => {
    stubAudioContext(4)
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      arrayBuffer: async () => new ArrayBuffer(16),
    } as unknown as Response)
    const onMove = vi.fn()
    render(
      <PremixTimeline
        src="/api/media/outputs/tts_a.wav"
        seconds={4}
        markers={[{ id: "boom", label: "💥", at_s: 1 }]}
        onMove={onMove}
      />,
    )
    const marker = await screen.findByRole("slider")
    fireEvent.keyDown(marker, { key: "ArrowRight" })
    expect(onMove).toHaveBeenCalledWith("boom", 1.1)
  })

  it("★ 拉不到音频时写明原因，且不装作在加载（秒数框仍可用）", async () => {
    stubAudioContext(4)
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"))
    render(
      <PremixTimeline
        src="/api/media/outputs/tts_gone.wav"
        seconds={4}
        markers={[{ id: "boom", label: "💥", at_s: 0 }]}
        onMove={() => {}}
      />,
    )
    await waitFor(() => expect(screen.getByText(/波形取不到/)).toBeInTheDocument())
    expect(screen.getByText(/offline/)).toBeInTheDocument()
    // 记号还在：拖不动也一样能看得见、能在秒数框里改
    expect(screen.getByRole("slider")).toBeInTheDocument()
  })

  it("★ 环境没有 Web Audio 也是**可读的一句**，不是一片空白", async () => {
    // 没装 AudioContext 时绝不能先跑 fetch：那会把整段 wav 拉下来却没有解码器。
    const fetchSpy = vi.spyOn(globalThis, "fetch")
    render(
      <PremixTimeline
        src="/api/media/outputs/tts_a.wav"
        seconds={4}
        markers={[{ id: "boom", label: "💥", at_s: 0 }]}
        onMove={() => {}}
      />,
    )
    await waitFor(() => expect(screen.getByText(/这个环境没有 Web Audio/)).toBeInTheDocument())
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
