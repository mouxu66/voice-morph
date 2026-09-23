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

/**
 * 展开某一行的「详情」。
 *
 * 第 7 步起 `pip:` / `requires:` / 探针快照默认折叠 —— 每张卡原本挂着 7 段元信息，
 * 那是开发者控制台不是设置页。**折叠 ≠ 删除**，所以下面凡是断言这些字段的用例，
 * 都得先点开。只有 `hasDetail` 为真的行才有这个按钮，所以 index 按渲染顺序数。
 */
function openDetail(index = 0) {
  const buttons = screen.getAllByRole("button", { name: "详情" })
  fireEvent.click(buttons[index])
}

describe("CapabilityPanel", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("关闭状态不渲染", () => {
    const { container } = render(<CapabilityPanel open={false} onClose={() => {}} />)
    expect(container).toBeEmptyDOMElement()
    expect(getPlugins).not.toHaveBeenCalled()
  })

  it("全 ok：有「已关闭的能力」空态，但不出现任何告警块", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND]))

    render(<CapabilityPanel open onClose={() => {}} />)

    // 「已关闭的能力」是一个常驻入口 —— 用户要能确认「确实没有东西被关掉」
    expect(await screen.findByText("已关闭的能力（0）")).toBeInTheDocument()
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
    // 要装什么也要露出来（pip 包名单独一个 code 节点，别和上面那条错误原文混在一起）——
    // 但第 7 步起这些字段默认折叠，先展开详情
    openDetail()
    expect(await screen.findByText("peft")).toBeInTheDocument()
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

    await screen.findByText("可用能力（1）")
    openDetail()
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

  it("健康探针的结果会摊开（后端不猜 ok，这里也不猜）", async () => {
    const base = catalog([OK_SOUND])
    getPlugins.mockResolvedValue({
      ...base,
      plugins: [
        {
          ...base.plugins[0],
          healthProbe: { ran: true, error: null, data: { ready: false, reason: "" } },
        },
      ],
    })

    render(<CapabilityPanel open onClose={() => {}} />)

    await screen.findByText("可用能力（1）")
    openDetail()
    // 空值（reason=""）不该摊 —— 否则每行都挂一串 `reason=`，噪声盖过信号
    expect(await screen.findByText(/实时状态：ready=false/)).toBeInTheDocument()
    expect(screen.queryByText(/reason/)).not.toBeInTheDocument()
  })

  it("探针没跑起来时说原因，不假装成功", async () => {
    const base = catalog([OK_SOUND])
    getPlugins.mockResolvedValue({
      ...base,
      plugins: [
        { ...base.plugins[0], healthProbe: { ran: false, error: "ImportError: comtypes", data: null } },
      ],
    })

    render(<CapabilityPanel open onClose={() => {}} />)

    await screen.findByText("可用能力（1）")
    openDetail()
    expect(await screen.findByText(/探针没跑起来：ImportError: comtypes/)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------- 第 7 步：界面语言
//
// 第 6 步把「能开关」做出来了，但那一屏是开发者控制台：每张卡底下挂着 7 段元信息、
// 16/19 项都挂一个绿徽章。这一步只改「怎么说」：
//   1. 沉默即正常 —— 可用不挂徽章，只有例外上色（否则噪声盖住真正的例外）；
//   2. 元信息折叠 —— 一条没丢，只是默认不出现；
//   3. 代价与收益同级 —— 用户来这一屏就是为了省；
//   4. 待重启是第四态 —— 点了开关界面没变化，用户只会以为开关坏了。

describe("CapabilityPanel · 状态语言", () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it("★ 可用态不挂徽章 —— 沉默即正常", async () => {
    getPlugins.mockResolvedValue(catalog([OK_CORE, OK_SOUND]))

    render(<CapabilityPanel open onClose={() => {}} />)

    await screen.findByText("可用能力（2）")
    // 16/19 项都挂一个「可用」绿徽章 = 把真正的例外埋掉，还和 notice 的成功色撞
    expect(screen.queryByText("可用")).not.toBeInTheDocument()
    // 全 ok 的清单里，连一个徽章都不该出现
    expect(screen.queryByText("已关闭")).not.toBeInTheDocument()
  })

  it("★ 缺依赖：徽章 + 缺口直接写在卡面上，不用去翻日志", async () => {
    getPlugins.mockResolvedValue(
      catalog([
        OK_CORE,
        {
          id: "sound.mine",
          name: "声音克隆",
          category: "sound",
          state: "broken",
          reasons: ["finetune: ModuleNotFoundError: peft"],
        },
      ]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText("未加载")).toBeInTheDocument()
    // 卡面上就要能读到「缺什么」，完整列表才在详情里
    expect(screen.getByText(/ModuleNotFoundError: peft/)).toBeInTheDocument()
  })

  it("★ 元信息默认折叠，点「详情」才出现（折叠 ≠ 删除）", async () => {
    getPlugins.mockResolvedValue(
      catalog([OK_CORE, { ...OK_SOUND, extras: { python: ["deepfilter"] }, requires: ["core.audio"] }]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    await screen.findByText("可用能力（2）")
    expect(screen.queryByText(/pip:/)).not.toBeInTheDocument()

    // OK_CORE 没有 extras/requires/reasons → 没有「详情」入口，所以这里只有一个按钮
    expect(screen.getAllByRole("button", { name: "详情" })).toHaveLength(1)
    openDetail()
    expect(await screen.findByText("deepfilter")).toBeInTheDocument()
    expect(screen.getByText(/依赖：core.audio/)).toBeInTheDocument()
  })

  it("★ 代价与收益同级：算出「关掉可省」", async () => {
    getPlugins.mockResolvedValue(
      catalog([
        OK_CORE,
        {
          ...OK_SOUND,
          extras: { models: [{ label: "Qwen3-TTS 权重", env: "VM_TTS_MODELS_DIR", size_hint_mb: 4900 }] },
        },
      ]),
    )

    render(<CapabilityPanel open onClose={() => {}} />)

    // 原实现只写「需要什么」（模型 4.9G），可用户来这一屏的目的恰恰是省
    expect(await screen.findByText("关掉可省 ≈4.8 GB")).toBeInTheDocument()
  })

  it("★ 改完立刻进「待重启」—— 不能只在面板中部写一句灰字", async () => {
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

    // ① 那一行自己立刻有反馈（否则用户以为开关坏了）
    expect(await screen.findByText("待重启")).toBeInTheDocument()
    // ② 顶部常驻一条，并给一个可执行出口
    expect(screen.getByText(/1 项能力有改动待重启/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /立即重启/ })).toBeInTheDocument()
  })

  it("★ 守卫给可执行出口：一并关闭依赖者，且顺序不能反", async () => {
    getPlugins.mockResolvedValue(
      catalog([OK_CORE, { ...OK_SOUND, blockedBy: ["sound.audiobook", "sound.effects"] }]),
    )
    setPluginEnabled.mockResolvedValue({
      id: "x",
      enabled: false,
      blockedBy: [],
      reason: "",
      restartRequired: true,
    })

    render(<CapabilityPanel open onClose={() => {}} />)

    // 以前只有一行灰字「想关它，先关掉这些」—— 把约束推给用户自己执行
    fireEvent.click(await screen.findByRole("button", { name: /一并关闭这 3 项/ }))

    await waitFor(() => expect(setPluginEnabled).toHaveBeenCalledTimes(3))
    // 先关依赖者、最后才是它自己；反了后端一定用 409 拦在第一步
    expect(setPluginEnabled.mock.calls.map((c) => c[0])).toEqual([
      "sound.audiobook",
      "sound.effects",
      "sound.tts",
    ])
  })
})
