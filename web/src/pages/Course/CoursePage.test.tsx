import { render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { CoursePage } from "@/pages/Course/CoursePage"
import type { useCourse } from "@/pages/Course/useCourse"
import type { CourseStatus, CourseVideoStatus } from "@/api/client"

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
    dirty: false,
    rawText: "",
    setRawText: vi.fn(),
    source: "",
    skipped: 0,
    fileName: "",
    gapMs: 350,
    setGapMs: vi.fn(),
    styleRefVoice: "",
    setStyleRefVoice: vi.fn(),
    sourcePptx: "",
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
    video: null,
    videoRunning: false,
    canExportVideo: false,
    startVideo: vi.fn(),
    cancelVideo: vi.fn(),
    videoFps: 30,
    setVideoFps: vi.fn(),
    videoGapMs: 350,
    setVideoGapMs: vi.fn(),
    forceSlides: false,
    setForceSlides: vi.fn(),
    ...over,
  }
}

const doneStatus = (pages: CourseStatus["pages"]): CourseStatus => ({
  running: false,
  status: "done",
  voice_id: "v1",
  style_ref_voice: "",
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

  it("★ 陈旧解析：讲稿改动后合成按钮禁用 + 提示重新解析", () => {
    // 不修的话：用户改完稿直接点合成，提交的是上一次的 pages ——
    // 录出来的旁白和眼前这份稿不是一回事。
    render(<CoursePage {...mkProps({ pages: [{ page: 1, text: "旧稿" }], dirty: true, canStart: false })} />)
    const btn = screen.getByRole("button", { name: /逐页合成/ }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(screen.getByText(/讲稿已改动，下面还是上一次解析的结果/)).toBeDefined()
  })

  it("error 态（worker 中断）已产出的页仍给时长清单入口", () => {
    const status: CourseStatus = {
      ...doneStatus([
        { page: 1, text: "第一页", url: "/u/1.wav", duration_s: 1, failed: false, error: "" },
        { page: 2, text: "第二页", url: "", duration_s: 0, failed: true, error: "RuntimeError" },
      ]),
      status: "error",
      error: "worker 崩了",
    }
    render(<CoursePage {...mkProps({ status })} />)
    expect(screen.getByRole("button", { name: /时长清单/ })).toBeDefined()
  })

  it("retry 中：进度文案区分「重试失败页」", () => {
    const status: CourseStatus = {
      ...doneStatus([
        { page: 1, text: "a", url: "/u/1.wav", duration_s: 1, failed: false, error: "" },
        { page: 2, text: "b", url: "", duration_s: 0, failed: true, error: "x" },
        { page: 3, text: "c", url: "/u/3.wav", duration_s: 1, failed: false, error: "" },
      ]),
      running: true,
      status: "running",
      done: 0,
      total: 1,
      percent: 0,
    }
    render(<CoursePage {...mkProps({ status, running: true })} />)
    expect(screen.getByText(/重试失败页 · 0\/1 页/)).toBeDefined()
  })
})

/**
 * 风格参考 ICL（2026-10-08 接入）。
 *
 * ⚠️ 这里守的是**措辞与代码行为一致**：`_tts_style_blocking` 拿 style_ref 当克隆
 * 参考、丢弃 ref_audio —— 实测输出频谱与参考音相关 0.989、与旁白音色参考音 0.932。
 * 所以文案不许写成"音色仍由旁白音色决定"（那是错的），必须点明音色也一起带过来。
 */
describe("CoursePage · 风格参考", () => {
  beforeEach(() => {
    document.body.innerHTML = ""
  })

  it("默认不启用，并说明不启用=纯声纹（语调偏平）", () => {
    render(<CoursePage {...mkProps({ pages: [{ page: 1, text: "x" }] })} />)
    const sel = screen.getByLabelText(/风格参考/) as HTMLSelectElement
    expect(sel.value).toBe("")
    expect(screen.getByText(/不启用（用上面选的旁白音色）/)).toBeDefined()
    expect(screen.getByText(/纯声纹克隆/)).toBeDefined()
  })

  it("★ 启用后文案必须点明「音色也一起带过来」，不能宣称音色仍由旁白音色决定", () => {
    render(
      <CoursePage {...mkProps({ pages: [{ page: 1, text: "x" }], styleRefVoice: "v1" })} />
    )
    expect(screen.getByText(/声音与语气/)).toBeDefined()
    expect(screen.getByText(/音色也一起带过来/)).toBeDefined()
    expect(screen.getByText(/旁白音色此时不参与发声/)).toBeDefined()
  })

  it("只列有参考音的音色：RVC 纯模型音色选了必 404，不能出现在下拉里", () => {
    const voices = [
      { id: "v1", display_name: "测试音色" },
      { id: "rvc_only", display_name: "纯模型音色", has_reference: false },
    ] as ReturnType<typeof useCourse>["voices"]
    render(<CoursePage {...mkProps({ pages: [{ page: 1, text: "x" }], voices })} />)
    const sel = screen.getByLabelText(/风格参考/) as HTMLSelectElement
    // 只该有「不启用」+ 有参考音那个；纯模型音色（rvc_only）不许出现
    expect(Array.from(sel.querySelectorAll("option")).map((o) => o.value)).toEqual(["", "v1"])
    expect(within(sel).getByRole("option", { name: /测试音色/ })).toBeDefined()
    expect(within(sel).queryByRole("option", { name: /纯模型音色/ })).toBeNull()
  })
})

/**
 * 导出成片（2026-10-08 接入，course_video.py）。
 *
 * ⚠️ 这里守的是「能出片」的界面前提：画面来自**原课件**（source.pptx），
 * 贴文本的批次必须解释原因而不是给一个点了必 400 的按钮。
 */
describe("CoursePage · 导出成片", () => {
  const videoStatus = (over: Partial<CourseVideoStatus> = {}): CourseVideoStatus => ({
    running: false,
    status: "idle",
    step: "",
    percent: 0,
    message: "",
    job_dir: "course_123",
    out_url: "",
    out_name: "course.mp4",
    duration_s: 0,
    pages: 0,
    slides: 0,
    fps: 30,
    gap_ms: 350,
    error: "",
    ...over,
  })

  beforeEach(() => {
    document.body.innerHTML = ""
  })

  it("贴文本的批次：按钮禁用并解释「没有课件文件」，不出给必 400 的按钮", () => {
    render(<CoursePage {...mkProps({ pages: [{ page: 1, text: "x" }], sourcePptx: "" })} />)
    const btn = screen.getByRole("button", { name: /导出成片/ }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(screen.getByText(/粘贴的文本/)).toBeDefined()
    expect(screen.getByText(/上传 \.pptx 重新解析/)).toBeDefined()
  })

  it("运行中：进度按 step 区分「导出画面/合成视频」+ 可取消", () => {
    render(
      <CoursePage
        {...mkProps({
          sourcePptx: "course_src_abc.pptx",
          canExportVideo: false,
          videoRunning: true,
          video: videoStatus({ running: true, status: "running", step: "mux", percent: 72, message: "正在把画面与旁白合成视频…" }),
        })}
      />
    )
    expect(screen.getByText(/合成视频 · 正在把画面与旁白合成视频/)).toBeDefined()
    expect(screen.getByText("72%")).toBeDefined()
    expect(screen.getByRole("button", { name: /取消导出/ })).toBeDefined()
  })

  it("error 态：报「导出成片失败」并把工具那一行的原文送到界面", () => {
    render(
      <CoursePage
        {...mkProps({
          sourcePptx: "course_src_abc.pptx",
          video: videoStatus({ status: "error", error: "导出画面失败（退出码 1）：PowerPoint 弹了模态框" }),
        })}
      />
    )
    expect(screen.getByText("导出成片失败")).toBeDefined()
    expect(screen.getByText(/PowerPoint 弹了模态框/)).toBeDefined()
  })

  it("done 态：出播放器 + 下载链接（时长与页数进徽标）", () => {
    render(
      <CoursePage
        {...mkProps({
          sourcePptx: "course_src_abc.pptx",
          canExportVideo: true,
          video: videoStatus({
            status: "done",
            out_url: "/api/media/outputs/course_123/course.mp4",
            duration_s: 12.34,
            pages: 2,
          }),
        })}
      />
    )
    expect(screen.getByText(/12\.3s · 2 页画面/)).toBeDefined()
    const player = screen.getByLabelText("讲课视频预览") as HTMLVideoElement
    expect(player.src).toContain("/api/media/outputs/course_123/course.mp4")
    const dl = screen.getByRole("link", { name: /下载 mp4/ }) as HTMLAnchorElement
    expect(dl.getAttribute("download")).toBe("course.mp4")
  })
})
