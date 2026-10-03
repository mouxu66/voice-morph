/**
 * 快捷短语板卡片的「热键角标」（2026-10-03，二期）。
 *
 * 前 5 条短语是全局热键 Alt+1~5 的目标（直播/放映中按下、播进虚拟声卡）。
 * 角标是这个能力唯一的界面提示 —— 漏渲染或标错序号，老师就不知道该按哪个。
 * 守两条：
 *  1. 前 5 条各带 Alt+1..Alt+5，**序号 = 列表顺序**（热键侧就是按清单序取的，
 *     见 main.cjs playPhraseByHotkey —— 两边必须同序）；
 *  2. 第 6 条起不再有角标（热键只绑 5 条）。
 */
import { render, screen } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

const fakeHook = vi.hoisted(() => ({
  items: [] as { id: string; text: string }[],
  activeId: "",
  error: "",
  hasVoice: true,
  refresh: vi.fn(),
  add: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  play: vi.fn(),
  stop: vi.fn(),
}))

vi.mock("@/pages/Tts/usePhraseBoard", () => ({ usePhraseBoard: () => fakeHook }))

const { PhraseBoardCard } = await import("./PhraseBoardCard")

beforeEach(() => {
  fakeHook.items = Array.from({ length: 6 }, (_, i) => ({ id: `id${i + 1}`, text: `第${i + 1}条` }))
})

describe("PhraseBoardCard · 热键角标", () => {
  it("前 5 条各带 Alt+1~Alt+5 角标", () => {
    render(<PhraseBoardCard />)
    for (let n = 1; n <= 5; n++) {
      expect(screen.getByText(`Alt+${n}`)).toBeTruthy()
    }
  })

  it("第 6 条起没有角标（热键只绑前 5 条）", () => {
    render(<PhraseBoardCard />)
    expect(screen.queryByText("Alt+6")).toBeNull()
  })

  it("提示文案点明全局热键与虚拟声卡的分工", () => {
    render(<PhraseBoardCard />)
    expect(screen.getByText(/Alt\+1~5/)).toBeTruthy()
    expect(screen.getByText(/虚拟声卡/)).toBeTruthy()
  })
})
