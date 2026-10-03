/**
 * `PetGuide` 的**定位**守卫 —— 钉住 2026-10-03 那次遮挡修复的结构性结论。
 *
 * 背景：导览桌宠的展开态曾经是 `fixed` 浮层，贴在主内容区右上角。
 * `PageShell` 是 `max-w-[1240px]` 居中，1440px 视口下右侧只剩 16px 空隙，
 * 而展开态要占 ~400px → 实测盖住三页右侧栏的小节标题（"本次会话" 87%、
 * "使用提示" 76%、"迭代筛选" 92%）。
 *
 * 中间试过「给 `<main>` 加 `padding-right: 400px`」，遮挡确实归零，
 * 但**每行文字都重新换行**，「我的音色」页从 6.7 屏涨到 8 屏 —— 横向重排比遮挡更伤。
 * 最终方案：展开态回到**文档流**里的顶部横幅（内容只被向下推，不换行），
 * 收起态才 `fixed` 到右下角。
 *
 * 这个坑的性质是「注释与行为不一致，所以 bug 活下来了」：
 * 老注释写着「完全脱离任何按钮区」，而那只对**收起态**成立，展开态恰恰贴着右侧栏。
 * 所以下面不测像素，只测**两条定位契约**：
 *   ① 展开态绝不能是 `fixed`（一 fixed 回去就必然重叠）；
 *   ② 收起态必须 `fixed` 到右下角（否则 64px 头像会跟着文档流躺在页面中间）。
 * 这两条任一被改坏，本测试立刻红 —— 比「注释里写清楚」可靠。
 */
import { fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

// `@/lib/electron` 是 IPC 门面，本用例不碰它，整套用桩替掉。
vi.mock("@/lib/electron", () => ({
  petGuide: vi.fn(),
}))

import { PetGuide } from "./PetGuide"

/** 展开态 = 还没触发自动收起的初始状态（首屏渲染即展开） */
function renderExpanded(page = "/voices") {
  const utils = render(<PetGuide page={page} enabled />)
  return utils
}

/** 找到导览容器：展开态是横幅、收起态是右下角那个包装 div */
function guideRoot(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('[data-testid="pet-guide"]')
  expect(el).not.toBeNull()
  return el as HTMLElement
}

afterEach(() => {
  vi.useRealTimers()
})

describe("PetGuide 定位契约", () => {
  it("展开态在文档流里（不是 fixed）—— fixed 回去就会压住右侧栏小节标题", () => {
    const { container } = renderExpanded()
    const root = guideRoot(container)
    const cls = root.className

    expect(cls).not.toMatch(/\bfixed\b/)
    // 横幅自己占一行，把内容整体往下推
    expect(cls).toMatch(/\bw-full\b/)
  })

  it("收起态 fixed 到右下角，且不跟文档流走", () => {
    const { container } = renderExpanded()
    expect(guideRoot(container).className).not.toMatch(/\bfixed\b/)

    // 点「跳过」= 收起（组件里同时清了定时器）。
    // 注意不能缓存 `root` 引用：切到收起态时 React 会换成**另一个**元素，
    // 老引用还指着已卸载的横幅节点，断言会对着旧 className 空转。
    fireEvent.click(screen.getByText("跳过"))

    const collapsed = guideRoot(container)
    expect(collapsed.className).toMatch(/\bfixed\b/)
    expect(collapsed.className).toMatch(/bottom-4/)
    expect(collapsed.className).toMatch(/right-4/)
    // 收起后气泡整块消失，只剩头像
    expect(screen.queryByText("跳过")).toBeNull()
  })

  it("横幅宽度与 PageShell 对齐（max-w-[1240px]），不显得外挂", () => {
    const { container } = renderExpanded()
    const root = guideRoot(container)
    // PageShell: `mx-auto w-full max-w-[1240px] px-6 lg:px-10`
    expect(root.className).toMatch(/max-w-\[1240px\]/)
    expect(root.className).toMatch(/mx-auto/)
  })

  it("enabled=false 或页面无导览文案时完全不渲染", () => {
    const off = render(<PetGuide page="/voices" enabled={false} />)
    expect(off.container.querySelector('[data-testid="pet-guide"]')).toBeNull()

    // /ytdlp 不在 GUIDES 里
    const none = render(<PetGuide page="/ytdlp" enabled />)
    expect(none.container.querySelector('[data-testid="pet-guide"]')).toBeNull()
  })
})
