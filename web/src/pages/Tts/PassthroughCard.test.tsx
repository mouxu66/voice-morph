/**
 * 真声直讲卡片（`PassthroughCard.tsx`）的**提示可达性**不变量。
 *
 * 这张卡片的成败几乎全在**一段文字上**：声音是送进了虚拟声卡，但 QQ/微信默认
 * 听的是物理麦克风 —— 用户不在通话软件里把麦克风改成 `CABLE Output`，功能就
 * "看起来完全没生效"（对面还是听到真麦）。
 *
 * 所以这条守卫钉的不是样式，而是：
 *   1. 那段**外部软件设置提示**必须**始终**可见（不是只在开启后才显示，
 *      也不是塞进 tooltip）—— 提前知道要改什么，比开完了才被告知有用；
 *   2. 提示里必须给出**具体设备名**（只说"改一下麦克风"等于没说）；
 *   3. 开着的时候要把**两个设备名**摊开（送往哪 / 从哪采）—— 出问题时这是
 *      第一手线索，也是用户唯一能自己判断"到底通没通"的依据。
 */
import { render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useAppStore } from "@/store/useAppStore"
import type * as clientModule from "@/api/client"

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof clientModule>("@/api/client")
  return {
    ...actual,
    passthroughStart: vi.fn(),
    passthroughStop: vi.fn(),
    passthroughStatus: vi.fn(),
  }
})

const client = await import("@/api/client")
const passthroughStatus = vi.mocked(client.passthroughStatus)
const passthroughStart = vi.mocked(client.passthroughStart)

const { PassthroughCard } = await import("@/pages/Tts/PassthroughCard")

const OFF = { ok: true, running: false, device: "", input_device: "" }
const ON = {
  ok: true,
  running: true,
  device: "扬声器 (VB-Audio Virtual Cable)",
  input_device: "麦克风阵列 (Senary Audio)",
}

beforeEach(() => {
  vi.clearAllMocks()
  useAppStore.setState({ backendUp: true })
  passthroughStatus.mockResolvedValue(OFF)
  passthroughStart.mockResolvedValue({ ok: true, running: true, device: ON.device })
})

describe("PassthroughCard · 外部软件设置提示", () => {
  it("★ 未开启时就已显示「去通话软件改麦克风」，且给出具体设备名", () => {
    render(<PassthroughCard />)

    // 这句话必须**不需要任何交互**就能看到 —— 提前知道 > 开完才被告知
    expect(document.body.textContent).toContain("CABLE Output")
    expect(document.body.textContent).toContain("麦克风")
    expect(document.body.textContent).toContain("通话软件")
  })

  it("★ 设备名是完整可复制的那个串（只说'改一下麦克风'等于没说）", () => {
    render(<PassthroughCard />)
    expect(document.body.textContent).toContain("CABLE Output (VB-Audio Virtual Cable)")
  })

  it("提示里点明「不选就白改」—— 否则用户会以为功能坏了", () => {
    render(<PassthroughCard />)
    const t = document.body.textContent ?? ""
    expect(t).toMatch(/物理麦克风|改了等于没改|还是/)
  })
})

describe("PassthroughCard · 运行态", () => {
  it("★ 开着时把「送往哪 / 从哪采」都摊开（用户唯一能自查的依据）", async () => {
    passthroughStatus.mockResolvedValue(ON)
    render(<PassthroughCard />)

    await waitFor(() => expect(document.body.textContent).toContain(ON.device))
    expect(document.body.textContent).toContain(ON.input_device)
    expect(document.body.textContent).toContain("送往")
    expect(document.body.textContent).toContain("采集")
  })

  it("关着时不显示运行态那两块（避免用户以为在送）", async () => {
    render(<PassthroughCard />)
    await waitFor(() => expect(passthroughStatus).toHaveBeenCalled())
    await waitFor(() => {
      expect(document.body.textContent).not.toContain("正在把真声送进虚拟声卡")
    })
  })

  it("按钮是 aria-pressed 二态（读屏/自动化可判当前状态）", async () => {
    render(<PassthroughCard />)
    const btn = screen.getByRole("button")
    expect(btn).toHaveAttribute("aria-pressed", "false")

    passthroughStatus.mockResolvedValue(ON)
    ;(await import("@testing-library/react")).fireEvent.click(btn)

    await waitFor(() => expect(screen.getByRole("button")).toHaveAttribute("aria-pressed", "true"))
  })

  it("★ 互斥说明必须写在卡片上（否则用户只会看到「点了没反应」）", () => {
    render(<PassthroughCard />)
    const t = document.body.textContent ?? ""
    expect(t).toContain("实时变声")
    expect(t).toContain("级联")
    expect(t).toContain("互斥")
  })
})
