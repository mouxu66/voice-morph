/**
 * `SetupBanner` 的降级提示逻辑 —— 这是本项目**第一个**前端单测。
 *
 * 为什么从它开始：`npm test` 此前是死脚本（`web/src` 下一个测试文件都没有、
 * `jsdom` 也从不在 lockfile 里），而前端唯一一处「静默失败」风险恰好就落在这个横幅上。
 *
 * 守的是这条**很容易写错**的分支：横幅的显隐判据**不能**是
 * `!status || status.allOk` —— 「后端配置齐全」与「后端能力都挂上了」是两件事。
 * 开着 `status.allOk` 短路时，只要用户的模型/环境都配好，**能力加载失败就永远不会提示**
 * （`caps.broken` 非空却被提前 return 掉），等于这次容错白做。
 * 2026-09-19 加能力提示时就踩在这个边上，所以专门钉一条。
 */
import { render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const getSetupStatus = vi.fn()
const getCapabilities = vi.fn()

// `@/lib/electron` 是 IPC 门面，整套用桩替掉（它的导出只在本文件里被组件读）。
vi.mock("@/lib/electron", () => ({
  getSetupGuides: vi.fn(),
  getSetupStatus: (...a: unknown[]) => getSetupStatus(...a),
  hasSetup: () => true,
  hasSetupScan: () => true,
  openGuideLink: vi.fn(),
  pickSetupDir: vi.fn(),
  restartBackend: vi.fn(),
  saveSetup: vi.fn(),
  scanSetup: vi.fn(),
  showSetupConfig: vi.fn(),
}))

vi.mock("@/api/client", () => ({
  getCapabilities: (...a: unknown[]) => getCapabilities(...a),
}))

import { SetupBanner } from "./ModelSetupPanel"

/** 配置齐全的 SetupStatus（本用例集只关心 allOk 与 items） */
const SETUP_ALL_OK = { allOk: true, items: [], config: {} } as never

const CAPS_ALL_OK = { ok: true, loaded: 26, total: 26, broken: [] }

/** 一个路由模块没挂上（例如缺可选依赖） */
const CAPS_ONE_BROKEN = {
  ok: false,
  loaded: 25,
  total: 26,
  broken: [{ module: "seed_vc", purpose: "router", reason: "RuntimeError: 缺依赖" }],
}

describe("SetupBanner", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("配置齐全 + 能力全加载 → 不出现", async () => {
    getSetupStatus.mockResolvedValue(SETUP_ALL_OK)
    getCapabilities.mockResolvedValue(CAPS_ALL_OK)

    const { container } = render(<SetupBanner onOpen={() => {}} />)
    await waitFor(() => expect(getCapabilities).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it("★ 配置齐全但有能力没挂上 → 必须出现（别被 allOk 短路掉）", async () => {
    getSetupStatus.mockResolvedValue(SETUP_ALL_OK)
    getCapabilities.mockResolvedValue(CAPS_ONE_BROKEN)

    render(<SetupBanner onOpen={() => {}} />)

    expect(await screen.findByText(/1 个能力未加载/)).toBeInTheDocument()
    // 模块名要露出来，用户才知道少了哪一块能力
    expect(screen.getByText(/seed_vc/)).toBeInTheDocument()
  })

  it("能力清单取不到（旧版后端 / 刚启动）→ 不抛错，且照样能提示缺配置", async () => {
    getSetupStatus.mockResolvedValue({
      allOk: false,
      items: [{ key: "tts_models", ok: false, label: "TTS 模型" }],
      config: {},
    } as never)
    getCapabilities.mockRejectedValue(new Error("404"))

    render(<SetupBanner onOpen={() => {}} />)

    expect(await screen.findByText(/TTS 未配置/)).toBeInTheDocument()
    // 能力清单挂了不该连带把「没有能力问题」误报出来
    expect(screen.queryByText(/个能力未加载/)).not.toBeInTheDocument()
  })

  it("有能力没挂上且给了「看诊断」去处时，多一个入口", async () => {
    getSetupStatus.mockResolvedValue(SETUP_ALL_OK)
    getCapabilities.mockResolvedValue(CAPS_ONE_BROKEN)

    render(<SetupBanner onOpen={() => {}} onDiagnose={() => {}} />)

    expect(await screen.findByRole("button", { name: "看诊断" })).toBeInTheDocument()
  })
})
