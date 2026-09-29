/**
 * `LiveScenePacks` 渲染测试 —— 守场景卡片**三条会说谎的地方**。
 *
 * 为什么值得测（这三条都不会报错，只会让用户看到假信息）：
 *   1. **高亮错了场景**：gaming / wechat / meeting 在后端的设置
 *      （perf_profile=game + denoise=True）**完全一样**。如果组件图省事
 *      用"设置最像哪个"来判高亮，点了「微信语音」的用户会看到「开黑」亮着。
 *      组件必须只认后端给的 active 键。
 *   2. **手动改过还高亮**：active 为 null 表示用户已偏离所有场景。
 *      这时必须**一个都不亮**，否则是在骗用户"你还在开黑模式"。
 *   3. **应用中还能重复点**：应用会重启变声（秒级）。不禁用的话用户会连点，
 *      后端并发重启会打架（声卡 restore/apply 交错）。
 */
import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import { LiveScenePacks } from "./LiveScenePacks"
import type { LiveScene } from "@/api/client"

const SCENES: LiveScene[] = [
  {
    key: "gaming", label: "开黑", desc: "边打游戏边变声。", icon: "Gamepad2", order: 10,
    in_settings: { perf_profile: "game", denoise: true },
    on_start: { monitor: false, subtitle: false },
  },
  {
    key: "wechat", label: "微信语音", desc: "发语音消息。", icon: "MessageCircle", order: 20,
    in_settings: { perf_profile: "game", denoise: true },
    on_start: { monitor: false, subtitle: false },
  },
  {
    key: "stream", label: "直播", desc: "音质优先。", icon: "Radio", order: 30,
    in_settings: { perf_profile: "balanced", denoise: true },
    on_start: { monitor: true, subtitle: true },
  },
]

function setup(over: Partial<Parameters<typeof LiveScenePacks>[0]> = {}) {
  const onApply = vi.fn()
  const utils = render(
    <LiveScenePacks scenes={SCENES} active={null} onApply={onApply} {...over} />,
  )
  return { onApply, ...utils }
}

describe("LiveScenePacks", () => {
  it("渲染全部场景的标签与说明", () => {
    setup()
    for (const s of SCENES) {
      expect(screen.getByText(s.label)).toBeTruthy()
      expect(screen.getByText(s.desc)).toBeTruthy()
    }
  })

  it("active 为 null 时一个都不高亮", () => {
    setup({ active: null })
    for (const s of SCENES) {
      expect(screen.getByTestId(`scene-${s.key}`).getAttribute("aria-pressed")).toBe("false")
    }
    expect(screen.queryByText("当前")).toBeNull()
  })

  it("★ 高亮只认 active 键，不被「设置相同」的场景抢走", () => {
    // gaming 与 wechat 的设置完全一样；active=wechat 时必须只有 wechat 亮
    setup({ active: "wechat" })
    expect(screen.getByTestId("scene-wechat").getAttribute("aria-pressed")).toBe("true")
    expect(screen.getByTestId("scene-gaming").getAttribute("aria-pressed")).toBe("false")
    expect(screen.getByTestId("scene-stream").getAttribute("aria-pressed")).toBe("false")
  })

  it("点击回调带上场景键", () => {
    const { onApply } = setup()
    fireEvent.click(screen.getByTestId("scene-stream"))
    expect(onApply).toHaveBeenCalledWith("stream")
  })

  it("★ 有场景在应用时，其它卡片被禁用（防并发重启）", () => {
    setup({ pending: "stream" })
    expect((screen.getByTestId("scene-gaming") as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByTestId("scene-wechat") as HTMLButtonElement).disabled).toBe(true)
    // 正在应用的那个也可点（但会转圈），实际被 disabled 是因为 isPending
    expect((screen.getByTestId("scene-stream") as HTMLButtonElement).disabled).toBe(true)
  })

  it("disabled=true 时全部禁用", () => {
    setup({ disabled: true })
    for (const s of SCENES) {
      expect((screen.getByTestId(`scene-${s.key}`) as HTMLButtonElement).disabled).toBe(true)
    }
  })

  it("空清单不渲染任何东西", () => {
    const { container } = render(
      <LiveScenePacks scenes={[]} active={null} onApply={vi.fn()} />,
    )
    expect(container.querySelector("[data-scene]")).toBeNull()
  })

  it("未知 icon 名不崩（后端图标名拼错是真实风险）", () => {
    const weird: LiveScene[] = [{ ...SCENES[0], icon: "NotARealIcon" }]
    expect(() => render(<LiveScenePacks scenes={weird} active={null} onApply={vi.fn()} />)).not.toThrow()
  })
})
