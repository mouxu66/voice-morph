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
import { render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const getPlugins = vi.fn()

vi.mock("@/api/client", () => ({
  getPlugins: (...a: unknown[]) => getPlugins(...a),
}))

import { CapabilityPanel } from "./CapabilityPanel"

type Entry = {
  id: string
  name: string
  category: string
  state: string
  reasons?: string[]
  summary?: string
  disableNote?: string
  extras?: Record<string, unknown>
  requires?: string[]
}

function catalog(entries: Entry[]) {
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
      routers: [],
      routes: [],
      legacyRoutes: [],
      extras: e.extras ?? {},
      health: null,
      disableNote: e.disableNote ?? "",
      ...e,
    })),
    loaders: { routers: 26, loaded: 26, broken: [] },
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

  it("清单取不到时报错但不崩", async () => {
    getPlugins.mockRejectedValue(new Error("Failed to fetch"))

    render(<CapabilityPanel open onClose={() => {}} />)

    expect(await screen.findByText("读取能力清单失败")).toBeInTheDocument()
    await waitFor(() => expect(getPlugins).toHaveBeenCalled())
  })
})
