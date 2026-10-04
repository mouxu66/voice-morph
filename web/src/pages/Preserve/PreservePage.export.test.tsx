/**
 * PreservePage 的「导出评测实验」入口（2026-10-04，产品 → 研究的桥）。
 *
 * 守三条容易写错的语义：
 *  1. 按钮显隐与「去复现」同口径 = **同意 + 至少一档参考音** ——
 *     它们是同一类"数据要离开档案"的动作，门禁条件不该两套。
 *  2. 点击成功后**必须把导出路径显示出来** —— 用户要拿这个路径去跑
 *     measure/analyze，只弹个"成功"等于没说。
 *  3. `exportExperiment` 返回 null（后端门禁 400 转成 error 状态）→ **不显示路径**，
 *     错误留在本页展示 —— 不能把失败当成功给一个假路径。
 *
 * 真正的同意门禁在后端（`voicepreserve_api.preserve_export`，见
 * `test_voicepreserve.py::TestExportApi`）；这里只守前端的引导与展示语义。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const { navigate, reproduce, exportExperiment, setPluginEnabled, reloadCatalog, catalogRef } =
  vi.hoisted(() => ({
    navigate: vi.fn(),
    reproduce: vi.fn(),
    exportExperiment: vi.fn(),
    setPluginEnabled: vi.fn(),
    reloadCatalog: vi.fn(),
    catalogRef: { current: null as unknown },
  }))

vi.mock("react-router-dom", () => ({ useNavigate: () => navigate }))

vi.mock("@/lib/pluginRoutes", async () => {
  const actual = (await vi.importActual("@/lib/pluginRoutes")) as Record<string, unknown>
  return {
    ...actual,
    usePluginCatalog: () => ({
      state: catalogRef.current
        ? { status: "ready", catalog: catalogRef.current }
        : { status: "loading" },
      reload: reloadCatalog,
    }),
  }
})

vi.mock("@/api/client", async () => {
  const actual = (await vi.importActual("@/api/client")) as Record<string, unknown>
  return { ...actual, setPluginEnabled }
})

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
    exportExperiment,
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

describe("PreservePage 导出评测实验", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("同意 + 有参考音 → 按钮出现", () => {
    render(<PreservePage p={fakeP()} />)
    expect(screen.getByRole("button", { name: /导出评测实验/ })).toBeTruthy()
  })

  it("没同意 → 按钮不出现（数据离开档案库之前就要有授权）", () => {
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
    expect(screen.queryByRole("button", { name: /导出评测实验/ })).toBeNull()
  })

  it("点击成功 → 显示导出路径（用户要拿它跑 measure/analyze）", async () => {
    exportExperiment.mockResolvedValue({
      ok: true,
      exp: "pres_p1234567890abcdef",
      root: "D:/变声/outputs/voicebank/pres_p1234567890abcdef",
      n_refs: 4,
      n_truth: 4,
      missing: [],
      measure_hint: "python tools/voicebank_eval.py measure --exp x",
    })
    render(<PreservePage p={fakeP()} />)
    fireEvent.click(screen.getByRole("button", { name: /导出评测实验/ }))
    await waitFor(() =>
      expect(screen.getByText(/pres_p1234567890abcdef/)).toBeTruthy(),
    )
  })

  it("导出失败（返回 null）→ 不显示任何路径，不把失败当成功", async () => {
    exportExperiment.mockResolvedValue(null)
    render(<PreservePage p={fakeP()} />)
    fireEvent.click(screen.getByRole("button", { name: /导出评测实验/ }))
    await waitFor(() => expect(exportExperiment).toHaveBeenCalled())
    expect(screen.queryByText(/已导出到/)).toBeNull()
  })
})
