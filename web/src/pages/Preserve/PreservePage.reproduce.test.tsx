/**
 * PreservePage 的「去复现」入口（2026-10-03，闭环补全）。
 *
 * 守三条容易写错的语义：
 *  1. 按钮显隐 = **同意 + 至少一档参考音**（结构性事实），不是 gate.verdict ——
 *     gate 没点过（null）时按钮也必须在，否则用户录完音对着一个空卡片发呆。
 *  2. 点击成功后**必须导航到 /tts**（复现的落点）—— 只注册不跳转，闭环断在半路。
 *  3. reproduce 返回 null（后端门禁 400 转成 error 状态）→ **不导航**，
 *     错误留在本页展示 —— 不能把失败当成成功把人送进输字页。
 *
 * 真正的同意门禁在后端（`voicepreserve.register_as_voice`，见
 * `test_voicepreserve.py::TestReproduce`）；这里只守前端的引导与跳转语义。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const { navigate, reproduce } = vi.hoisted(() => ({
  navigate: vi.fn(),
  reproduce: vi.fn(),
}))

vi.mock("react-router-dom", () => ({ useNavigate: () => navigate }))

import type { usePreserve } from "@/pages/Preserve/usePreserve"
import { PreservePage } from "./PreservePage"

type P = ReturnType<typeof usePreserve>

const GUIDE = {
  script: ["第一句，今天我们讲声音。", "第二句，谢谢大家。"],
  durations: [{ tag: "3s", duration_s: 3, sentences: 1, covered: true }],
  script_capacity_s: 91,
  consent: { version: "v1", statement: "我同意将我的录音仅用于本人声音复现。" },
}

function fakeP(overrides: Record<string, unknown> = {}): P {
  return {
    loading: false,
    error: null,
    notice: null,
    storeDir: "D:/media/preserve",
    guide: GUIDE,
    profiles: [],
    selectedId: "p1234567890abcdef",
    busy: "",
    recordingFor: null,
    recordSeconds: 0,
    events: [],
    gate: null,
    setError: vi.fn(),
    setNotice: vi.fn(),
    setSelectedId: vi.fn(),
    create: vi.fn(),
    remove: vi.fn(),
    consent: vi.fn(),
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    runGate: vi.fn(),
    reproduce,
    profile: {
      id: "p1234567890abcdef",
      label: "王老师",
      created_at: "2026-10-03T00:00:00",
      consent: { version: "v1", scope: "self", operator: "t" },
      refs: [{ tag: "10s", duration_s: 10, actual_s: 9.8, file: "10s.wav" }],
      truth: [],
    },
    ...overrides,
  } as unknown as P
}

describe("PreservePage 去复现", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("同意 + 有参考音 → 按钮出现（哪怕还没体检，gate 为 null）", () => {
    render(<PreservePage p={fakeP()} />)
    expect(screen.getByRole("button", { name: /去复现/ })).toBeTruthy()
  })

  it("没同意 → 按钮不出现（同意先于克隆，前端也要引导）", () => {
    render(
      <PreservePage
        p={fakeP({
          profile: {
            id: "p1234567890abcdef",
            label: "王老师",
            created_at: "2026-10-03T00:00:00",
            consent: null,
            refs: [{ tag: "10s", duration_s: 10, actual_s: 9.8, file: "10s.wav" }],
            truth: [],
          },
        })}
      />,
    )
    expect(screen.queryByRole("button", { name: /去复现/ })).toBeNull()
  })

  it("只有同意、没参考音 → 按钮也不出现", () => {
    render(
      <PreservePage
        p={fakeP({
          profile: {
            id: "p1234567890abcdef",
            label: "王老师",
            created_at: "2026-10-03T00:00:00",
            consent: { version: "v1", scope: "self", operator: "t" },
            refs: [],
            truth: [],
          },
        })}
      />,
    )
    expect(screen.queryByRole("button", { name: /去复现/ })).toBeNull()
  })

  it("点击 → 调用 reproduce，成功后导航到 /tts（闭环落点）", async () => {
    reproduce.mockResolvedValue({
      ok: true,
      voice_id: "preserve_p1234567890abcdef",
      label: "王老师",
      ref: "10s.wav",
      duration_s: 10,
    })
    render(<PreservePage p={fakeP()} />)
    fireEvent.click(screen.getByRole("button", { name: /去复现/ }))
    await waitFor(() => {
      expect(reproduce).toHaveBeenCalledTimes(1)
      expect(navigate).toHaveBeenCalledWith("/tts")
    })
  })

  it("reproduce 失败（后端门禁 400 → 返回 null）→ 不导航", async () => {
    reproduce.mockResolvedValue(null)
    render(<PreservePage p={fakeP()} />)
    fireEvent.click(screen.getByRole("button", { name: /去复现/ }))
    await waitFor(() => {
      expect(reproduce).toHaveBeenCalledTimes(1)
    })
    expect(navigate).not.toHaveBeenCalled()
  })

  it("录完音还没体检 → 显示「录音已就绪」引导（老师最常见的卡点：不知道下一步）", () => {
    render(<PreservePage p={fakeP()} />)
    expect(screen.getByText(/录音已经就绪/)).toBeTruthy()
  })

  it("录音中 → 脚本自动展开，不用再找「展开脚本」按钮", () => {
    render(<PreservePage p={fakeP({ recordingFor: 10, recordSeconds: 3 })} />)
    expect(screen.getByText(/第一句，今天我们讲声音/)).toBeTruthy()
    expect(screen.queryByRole("button", { name: /展开录音脚本/ })).toBeNull()
  })
})

