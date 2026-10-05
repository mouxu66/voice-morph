import { render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { CoursePage } from "@/pages/Course/CoursePage"
import type { useCourse } from "@/pages/Course/useCourse"
import type { CourseStatus } from "@/api/client"

/**
 * 讲稿录课页的渲染测试（jsdom）。
 * mock 掉 client 层（不发真请求）与插件目录（catalog 状态最简），
 * 只验证「状态 → 界面」的映射与文案红线。
 */

vi.mock("@/api/client", async () => {
  const actual = (await vi.importActual("@/api/client")) as Record<string, unknown>
  return { ...actual, mediaUrl: (p: string) => `http://test/${p}` }
})

vi.mock("@/store/useAppStore", () => ({
  useAppStore: () => ({
    backendUp: true,
    voices: [{ id: "v1", display_name: "测试音色" }],
    selectedVoiceId: "v1",
    selectVoice: vi.fn(),
  }),
}))

vi.mock("@/components/voice-studio/StudioAudioPlayer", () => ({
  StudioAudioPlayer: () => <div data-testid="player" />,
}))

vi.mock("@/lib/pluginRoutes", async () => {
  const actual = (await vi.importActual("@/lib/pluginRoutes")) as Record<string, unknown>
  return {
    ...actual,
    usePluginCatalog: () => ({ state: { status: "loading" }, reload: vi.fn() }),
  }
})

function mkProps(over: Partial<ReturnType<typeof useCourse>> = {}): ReturnType<typeof useCourse> {
  return {
    backendUp: true,
    voices: [{ id: "v1", display_name: "测试音色" }] as ReturnType<typeof useCourse>["voices"],
    selectedVoiceId: "v1",
    selectVoice: vi.fn(),
    pages: [],
    source: "",
    skipped: 0,
    fileName: "",
    gapMs: 350,
    setGapMs: vi.fn(),
    parsing: false,
    submitting: false,
    running: false,
    canStart: false,
    errorMessage: "",
    setErrorMessage: vi.fn(),
    status: null,
    loadFile: vi.fn(),
    loadText: vi.fn(),
    start: vi.fn(),
    cancel: vi.fn(),
    retry: vi.fn(),
    ...over,
  }
}

const doneStatus = (pages: CourseStatus["pages"]): CourseStatus => ({
  running: false,
  status: "done",
  voice_id: "v1",
  done: pages.length,
  total: pages.length,
  percent: 100,
  current_page: 0,
  current_text: "",
  job_dir: "course_123",
  gap_ms: 350,
  pages,
  error: "",
})

describe("CoursePage 渲染", () => {
  beforeEach(() => {
    document.body.innerHTML = ""
  })

  it("空状态：引导文案与禁用的合成按钮", () => {
    render(<CoursePage {...mkProps()} />)
    expect(screen.getByText("逐页音轨")).toBeDefined()
    expect(screen.getByText(/导入讲稿并提交后/)).toBeDefined()
    const btn = screen.getByRole("button", { name: /逐页合成/ }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })

  it("完成后：逐页清单 + 失败页标记 + 重试按钮", () => {
    const status = doneStatus([
      { page: 1, text: "第一页", url: "/api/media/outputs/course_123/page_001.wav", duration_s: 1.2, failed: false, error: "" },
      { page: 2, text: "第二页", url: "", duration_s: 0, failed: true, error: "RuntimeError: 模拟" },
    ])
    render(<CoursePage {...mkProps({ status, pages: [{ page: 1, text: "第一页" }] })} />)
    expect(screen.getByText("第 1 页")).toBeDefined()
    expect(screen.getByText("合成失败")).toBeDefined()
    expect(screen.getByText(/重试失败页（1）/)).toBeDefined()
    expect(screen.getByRole("button", { name: /时长清单/ })).toBeDefined()
  })

  it("运行中：进度与当前页", () => {
    const status: CourseStatus = {
      ...doneStatus([]),
      running: true,
      status: "running",
      done: 1,
      total: 3,
      percent: 33,
      current_page: 2,
      current_text: "例题讲解",
      pages: [],
    }
    render(<CoursePage {...mkProps({ status, running: true })} />)
    expect(screen.getByText(/逐页合成 · 1\/3 页/)).toBeDefined()
    expect(screen.getByText(/正在合成第 2 页/)).toBeDefined()
    expect(screen.getByText("33%")).toBeDefined()
  })

  it("skipped 提示：没写备注的页要引导补备注，而不是无声消失", () => {
    render(<CoursePage {...mkProps({ skipped: 2, pages: [{ page: 1, text: "x" }], source: "pptx" })} />)
    expect(screen.getByText(/2 页没有备注，已跳过/)).toBeDefined()
  })

  it("文案红线：不出现「师范实训/诊断/疗效」等越界措辞", () => {
    const { container } = render(
      <CoursePage {...mkProps({ status: doneStatus([{ page: 1, text: "x", url: "u", duration_s: 1, failed: false, error: "" }]) })} />
    )
    const html = container.innerHTML
    expect(html).not.toMatch(/师范实训|诊断|疗效|健康监测/)
  })
})
