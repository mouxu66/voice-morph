/**
 * PreservePage 的「声音随访」区（2026-10-05，纵向随访）。
 *
 * 守三条容易写错的语义：
 *  1. **趋势图必须跟着数据走** —— 有点画点（每点一个圆），缺值点如实跳过
 *     （指标算不出来的那次不该在图上冒出一个假高度）。
 *  2. **措辞红线** —— 这是「发声状态趋势参考」，不是医疗诊断：
 *     渲染出来的 DOM 不得出现「诊断 / 健康监测 / 疗效」三个词
 *     （后端 pytest 对 trend payload 守一遍，这里对最终界面守一遍）。
 *  3. **空状态要引导** —— 没有随访记录时给「录第一次」的引导文案，
 *     不许是一块空白。
 */
import { render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }))

vi.mock("react-router-dom", () => ({ useNavigate: () => navigate }))

vi.mock("@/lib/pluginRoutes", async () => {
  const actual = (await vi.importActual("@/lib/pluginRoutes")) as Record<string, unknown>
  return {
    ...actual,
    usePluginCatalog: () => ({
      state: { status: "loading" },
      reload: vi.fn(),
    }),
  }
})

import type { usePreserve } from "@/pages/Preserve/usePreserve"
import type { PreserveTrend } from "@/api/client"
import { PreservePage } from "./PreservePage"

type P = ReturnType<typeof usePreserve>

const GUIDE = {
  script: ["第一句，今天我们讲声音。", "第二句，谢谢大家。"],
  durations: [{ tag: "3s", duration_s: 3, sentences: 1, covered: true }],
  script_capacity_s: 91,
  consent: { version: "v1", statement: "我同意将我的录音仅用于本人声音复现。" },
}

function trendFixture(n: number, withValues = true): PreserveTrend {
  return {
    n,
    hint: "发声状态趋势参考：给自己看变化用的，数值不构成任何医疗建议。",
    points: Array.from({ length: n }, (_, i) => ({
      at: `2026-10-0${i + 1} 10:00:00`,
      at_ms: 1759600000000 + i * 86400000,
      file: `fu_00${i}.wav`,
      url: `/api/media/preserve/p1/followups/fu_00${i}.wav`,
      actual_s: 12.0,
      bytes: 500000,
      note: "",
      metrics: withValues
        ? { hnr: 18 + i, h1_h2: 4 + i, spectral_tilt: -3, jitter_local: 0.01,
            shimmer_local: 0.05, f0_median: 160, voiced_ratio: 0.8, duration_s: 12 }
        : null,
      lamps: withValues
        ? { lamps: [
            { label: "相似度", lamp: "green", detail: "x" },
            { label: "自然度", lamp: "green", detail: "x" },
            { label: "发声负荷", lamp: i === 2 ? "red" : "yellow", detail: "x" },
          ], verdict: "部分可用", tested_count: 3 }
        : null,
      emb_sim: withValues ? 0.8 + i * 0.01 : null,
      metrics_error: withValues ? "" : "读不出浊音段",
    })),
  }
}

function fakeP(trend: PreserveTrend | null, overrides: Record<string, unknown> = {}): P {
  return {
    loading: false,
    error: null,
    notice: null,
    storeDir: "D:/media/preserve",
    guide: GUIDE,
    profiles: [],
    selectedId: "p1234567890abcdef",
    busy: "",
    recordingFor: 0,
    recordingFollowup: false,
    recordSeconds: 0,
    events: [],
    gate: null,
    trend,
    setError: vi.fn(),
    setNotice: vi.fn(),
    setSelectedId: vi.fn(),
    create: vi.fn(),
    remove: vi.fn(),
    consent: vi.fn(),
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    startFollowupRecording: vi.fn(),
    runGate: vi.fn(),
    reproduce: vi.fn(),
    exportExperiment: vi.fn(),
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

describe("PreservePage 声音随访", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("有随访记录 → 三张趋势小图各画 n 个点", () => {
    const { container } = render(<PreservePage p={fakeP(trendFixture(3))} />)
    expect(screen.getByText("声音随访")).toBeTruthy()
    for (const label of ["相似度（与档案参考音）", "自然度（HNR）", "发声平稳度（H1-H2）"]) {
      const svg = container.querySelector(`svg[aria-label="${label}"]`)
      expect(svg).toBeTruthy()
      expect(svg!.querySelectorAll("circle").length).toBe(3)
    }
  })

  it("缺值点如实跳过 —— 指标没算出来的那次不冒出假高度", () => {
    const { container } = render(<PreservePage p={fakeP(trendFixture(2, false))} />)
    const svg = container.querySelector('svg[aria-label="自然度（HNR）"]')
    expect(svg).toBeTruthy()
    expect(svg!.querySelectorAll("circle").length).toBe(0)
    expect(svg!.textContent).toContain("暂无数值")
  })

  it("没有随访记录 → 显示引导文案，不是空白", () => {
    render(<PreservePage p={fakeP(null)} />)
    expect(screen.getByText(/还没有随访记录/)).toBeTruthy()
  })

  it("空趋势（n=0）同样走引导文案", () => {
    render(<PreservePage p={fakeP({ n: 0, points: [], hint: "x" })} />)
    expect(screen.getByText(/还没有随访记录/)).toBeTruthy()
  })

  it("接口下发的口径说明（hint）要显示给用户", () => {
    render(<PreservePage p={fakeP(trendFixture(1))} />)
    expect(screen.getByText(/发声状态趋势参考/)).toBeTruthy()
  })

  it("★★ 措辞红线：界面文案不得出现「诊断 / 健康监测 / 疗效」", () => {
    const { container } = render(<PreservePage p={fakeP(trendFixture(3))} />)
    const text = container.textContent ?? ""
    for (const bad of ["诊断", "健康监测", "疗效"]) {
      expect(text.includes(bad)).toBe(false)
    }
  })
})
