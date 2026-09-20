/**
 * `CapabilityPanel` 的三态展示 —— 守的是「被关掉的能力」这件事在界面上说得清。
 *
 * 为什么要单开一屏而不是并进 `SetupBanner`：横幅的显隐判据是「有 broken」，
 * 而 `disabled`（用户自己关的）**刻意不算 broken** —— 否则关一个能力就弹一条降级告警。
 * 代价是「关掉的能力」在界面上完全没有出口，功能凭空消失。所以这里补另一半：
 * 横幅讲「不可用」，本屏讲「被关掉的去哪了」。
 *
 * 三个容易写错的点，各钉一条：
 * 1. `disabled` 的项**照样要列 reasons**（要能回答「你关的，而且它本来就是坏的」）；
 * 2. `broken` 的原因必须露出来（否则用户只知道「少了块功能」，不知道缺什么）；
 * 3. 全 ok 时**不能**冒出告警块（否则等于把 disabled 混进 broken 的老毛病换个地方犯）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const getPlugins = vi.fn()
const setPluginEnabled = vi.fn()
const applyPluginPreset = vi.fn()

vi.mock("@/api/client", () => ({
  getPlugins: (...a: unknown[]) => getPlugins(...a),
  setPluginEnabled: (...a: unknown[]) => setPluginEnabled(...a),
  applyPluginPreset: (...a: unknown[]) => applyPluginPreset(...a),
}))

import { CapabilityPanel } from "./CapabilityPanel"

type Entry = {
  id: string
  name: string
  category: string
  state: string
  /** 缺省按 `state` 推（与后端一致：只有"被依赖而保留"时会不一样） */
  enabled?: boolean
  blockedBy?: string[]
  reasons?: string[]
  summary?: string
  disableNote?: string
  extras?: Record<string, unknown>
  requires?: string[]
}

function catalog(entries: Entry[], preset = "standard") {
  const counts = { total: entries.length, ok: 0, broken: 0, disabled: 0 }
  for (const e of entries) counts[e.state as "ok" | "broken" | "disabled"] += 1
  return {
    ok: counts.broken === 0,
    counts,
    plugins: entries.map((e) => ({
      kind: "builtin",
      order: 10,
      core: e.category === "core",
      summary: e.summary ?? "一句话说明",
      reasons: e.reasons ?? [],
      requires: e.requires ?? [],
      blockedBy: e.blockedBy ?? [],
      enabled: e.enabled ?? e.state !== "disabled",
      routers: [],
      routes: [],
      legacyRoutes: [],
      extras: e.extras ?? {},
      health: null,
      disableNote: e.disableNote ?? "",
      ...e,
    })),
    loaders: { routers: 26, loaded: 26, broken: [] },
    restartRequired: true,
    preset,
    presets: [
      { id: "light", label: "轻量", plugins: ["sound.offline-vc"] },
      { id: "standard", label: "标准", plugins: ["sound.tts", "sound.offline-vc"] },
      { id: "full", label: "全能", plugins: ["sound.tts", "sound.offline-vc", "sound.mine"] },
    ],
  }
}

const OK_CORE: Entry = { id: "core.system", name: "系统与体检", category: "core", state: "ok" }
const OK_SOUND: Entry = { id: "sound.tts", name: "输字变声", category: "sound", state: "ok" }

describe("CapabilityPanel", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("关闭状态不渲染", () => {
    const { container } = render(<CapabilityPanel open={false} onClose={() => {}} />)
    expect(container).toBeEmptyDOMElement()
    expect(getPlugins).not.toHaveBeenCalled()
  })

  it("全 ok：有「被关掉的能力」空态，但不出现任何告警块", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND]))

    render(<CapabilityPanel open onClose={() => {}} />)

    // 「被关掉的能力」是一个常驻入口 —— 用户要能确认「确实没有东西被关掉」
    expect(await screen.findByText("被关掉的能力")).toBeInTheDocument()
    expect(screen.getByText("没有被关闭的能力。")).toBeInTheDocument()
    // 关键：不能因为存在 disabled 这个概念就冒出未加载告警
    expect(screen.queryByText(/未加载的能力/)).not.toBeInTheDocument()
    expect(screen.getByText("可用能力（2）")).toBeInTheDocument()
    // 加载器账本一起报出来，manifest 与加载器对不上时能一眼看出是哪一层
    expect(screen.getByText(/26 个路由模块，已挂 26 个/)).toBeInTheDocument()
  })

  it("★ 被关掉的能力照样列出原因（你关的，而且它本来就是坏的）", async () => {
    getPlugins.mockResolvedValue(
      catalog([
        OK_CORE,
        {
          id: "sound.tts",
          name: "输字变声",
          category: "sound",
          state: "disabled",
          reasons: ["warmup: RuntimeError: 缺依赖"],
        },
      ]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText(/1 已关闭/)).toBeInTheDocument()
    // 被关掉 ≠ 原因被丢掉
    expect(screen.getByText(/缺依赖/)).toBeInTheDocument()
    // 且不计入 broken
    expect(screen.queryByText(/未加载的能力/)).not.toBeInTheDocument()
  })

  it("未加载的能力单独成块，并露出原因与要装的东西", async () => {
    getPlugins.mockResolvedValue(
      catalog([
        OK_CORE,
        {
          id: "sound.mine",
          name: "声音克隆",
          category: "sound",
          state: "broken",
          reasons: ["finetune: ModuleNotFoundError: peft"],
          extras: { python: ["peft"] },
          requires: ["core.voices"],
        },
      ]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText("未加载的能力（1）")).toBeInTheDocument()
    expect(screen.getByText(/ModuleNotFoundError: peft/)).toBeInTheDocument()
    // 要装什么也要露出来（pip 包名单独一个 code 节点，别和上面那条错误原文混在一起）
    expect(screen.getByText("peft")).toBeInTheDocument()
    expect(screen.getByText(/依赖：core.voices/)).toBeInTheDocument()
    // broken 项不该混进「可用能力」
    expect(screen.getByText("可用能力（1）")).toBeInTheDocument()
  })

  it("核心能力的不可关说明会显示出来", async () => {
    getPlugins.mockResolvedValue(
      catalog([{ ...OK_CORE, disableNote: "核心：不可关闭（体检与存储看板是所有能力的问题出口）" }]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText(/核心：不可关闭/)).toBeInTheDocument()
  })

  it("★ extras.models 是对象数组 —— 渲染 label，绝不能出现 [object Object]", async () => {
    getPlugins.mockResolvedValue(
      catalog([
        {
          ...OK_SOUND,
          extras: {
            python: ["Pillow"],
            external: [{ kind: "dir", label: "RVC 整合包", env: "VM_RVC_ROOT", size_hint_mb: 1500 }],
            models: [
              {
                label: "Qwen3-TTS 权重（含 tokenizer 与参考音）",
                env: "VM_TTS_MODELS_DIR",
                size_hint_mb: 4900,
              },
            ],
          },
        },
      ]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText(/Qwen3-TTS 权重/)).toBeInTheDocument()
    expect(screen.getByText(/需自备：RVC 整合包/)).toBeInTheDocument()
    // 回归：这里曾把 models 按 string[] 声明 → 直接 join → 界面上是 [object Object]。
    // 单测的手写数据当时也是字符串，所以只有真机截图才暴露 —— 数据形状照抄后端。
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  })

  it("清单取不到时报错但不崩", async () => {
    getPlugins.mockRejectedValue(new Error("Failed to fetch"))

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText("读取能力清单失败")).toBeInTheDocument()
    await waitFor(() => expect(getPlugins).toHaveBeenCalled())
  })
})

// ---------------------------------------------------------------- 第 6 步：开关
//
// 上面守的是"看得清"，下面守的是"真的能改、改了说得清"。
// 三条最要紧的：
//   1. 开关读 `enabled`，不能拿 `state` 推（被依赖而保留的能力会显示错）；
//   2. 被拒时**原样**显示后端 detail（409 说的是"你还被谁依赖着"，
//      通用文案会把它翻译成"同名文件已存在"）；
//   3. 写完必须说"重启生效" —— 否则用户点了没变化，只会以为开关坏了。

describe("CapabilityPanel · 开关", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("★ 开关读 enabled，不是 state（被依赖而保留的能力必须显示成开着的）", async () => {
    getPlugins.mockResolvedValue(
      catalog([
        OK_CORE,
        // 用户关了它，但 sound.audiobook 还依赖它 → 后端仍保留
        { ...OK_SOUND, state: "disabled", enabled: true, blockedBy: ["sound.audiobook"] },
      ]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    const sw = await screen.findByRole("switch", { name: /关闭 输字变声/ })
    expect(sw).toHaveAttribute("aria-checked", "true")
    // 关不掉的原因要说出来，别让用户对着一个开关干瞪眼
    expect(screen.getByText(/被依赖：sound.audiobook/)).toBeInTheDocument()
  })

  it("核心能力的开关不给点", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE]))

    render(<CapabilityPanel open onClose={() => {}} />)

    const sw = await screen.findByRole("switch", { name: /核心能力，不可关闭 系统与体检/ })
    expect(sw).toBeDisabled()
  })

  it("点开关 → 调 enable/disable，并提示重启生效", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND]))
    setPluginEnabled.mockResolvedValue({
      id: "sound.tts",
      enabled: false,
      blockedBy: [],
      reason: "",
      restartRequired: true,
    })

    render(<CapabilityPanel open onClose={() => {}} />)

    const sw = await screen.findByRole("switch", { name: /关闭 输字变声/ })
    fireEvent.click(sw)

    await waitFor(() => expect(setPluginEnabled).toHaveBeenCalledWith("sound.tts", false))
    // 断言「已关闭…」这一条整体 —— 只匹配"重启应用后生效"会连页脚说明一起命中
    expect(await screen.findByText("已关闭「输字变声」，重启应用后生效。")).toBeInTheDocument()
  })

  it("★ 关闭守卫被拒时原样显示后端 detail（409 ≠ 同名文件已存在）", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND]))
    setPluginEnabled.mockRejectedValue(
      new Error("sound.tts 仍被 sound.audiobook 依赖；先关掉它们"),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    const sw = await screen.findByRole("switch", { name: /关闭 输字变声/ })
    fireEvent.click(sw)

    // 通用错误文案会把 409 翻译成"同名文件已存在，请换个名字"，在这里是完全错的
    expect(await screen.findByText(/仍被 sound.audiobook 依赖/)).toBeInTheDocument()
    expect(screen.queryByText(/同名文件/)).not.toBeInTheDocument()
  })

  it("套餐预设可点，点了调 applyPluginPreset", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND], "custom"))
    applyPluginPreset.mockResolvedValue({
      preset: "light",
      disabled: ["sound.tts"],
      enabled: ["sound.offline-vc"],
      restartRequired: true,
    })

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText(/当前是自定义组合/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: /轻量/ }))

    await waitFor(() => expect(applyPluginPreset).toHaveBeenCalledWith("light"))
    expect(await screen.findByText(/已套用「轻量」套餐/)).toBeInTheDocument()
  })

  it("当前套餐高亮（aria-pressed）", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND], "standard"))

    render(<CapabilityPanel open onClose={() => {}} />)

    const standard = await screen.findByRole("button", { name: /标准/ })
    expect(standard).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByRole("button", { name: /轻量/ })).toHaveAttribute("aria-pressed", "false")
  })
})
