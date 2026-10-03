/**
 * 快捷短语板 hook 的关键不变量。
 *
 * 1. **合成互斥用 ref**：同一 tick 连点两次只放行一次 —— state 当锁会穿透
 *    （setBusy 异步，双击两次都读到旧值），两次合成请求在冷态下要等几十秒。
 * 2. **合成锁与 CRUD 锁分离**：冷态合成最长要等几十秒，这期间"加一条短语"
 *    是无关动作，不该被冻住。
 * 3. **停止**：播错了要能立刻掐掉，activeId 必须归零（按钮高亮跟着消失）。
 */
import { renderHook, waitFor, act } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useAppStore } from "@/store/useAppStore"
import type * as clientModule from "@/api/client"

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof clientModule>("@/api/client")
  return {
    ...actual,
    voicePhraseList: vi.fn(),
    voicePhraseCreate: vi.fn(),
    voicePhraseUpdate: vi.fn(),
    voicePhraseDelete: vi.fn(),
    voicePhraseSpeak: vi.fn(),
  }
})

const client = await import("@/api/client")
const voicePhraseList = vi.mocked(client.voicePhraseList)
const voicePhraseCreate = vi.mocked(client.voicePhraseCreate)
const voicePhraseUpdate = vi.mocked(client.voicePhraseUpdate)
const voicePhraseDelete = vi.mocked(client.voicePhraseDelete)
const voicePhraseSpeak = vi.mocked(client.voicePhraseSpeak)

const { usePhraseBoard } = await import("@/pages/Tts/usePhraseBoard")

const ITEMS = [
  { id: "a1", text: "同学们，我们来看这道题。" },
  { id: "a2", text: "大家先思考一分钟。" },
]

beforeEach(() => {
  vi.clearAllMocks()
  // jsdom 的媒体方法未实现（会往控制台打 "Not implemented"），换成可控桩
  window.HTMLMediaElement.prototype.play = vi.fn(() => Promise.resolve())
  window.HTMLMediaElement.prototype.pause = vi.fn()
  useAppStore.setState({ backendUp: true, selectedVoiceId: "wang" })
  voicePhraseList.mockResolvedValue({ ok: true, items: ITEMS })
})

describe("usePhraseBoard · 加载与 CRUD", () => {
  it("backendUp 时自动拉清单", async () => {
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))
    expect(voicePhraseList).toHaveBeenCalledTimes(1)
  })

  it("add 剥首尾空白后追加；空串不打后端", async () => {
    voicePhraseCreate.mockResolvedValue({ ok: true, item: { id: "n1", text: "下课了。" } })
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    await result.current.add("   ")
    expect(voicePhraseCreate).not.toHaveBeenCalled()

    await result.current.add("  下课了。 ")
    expect(voicePhraseCreate).toHaveBeenCalledWith("下课了。")
    await waitFor(() =>
      expect(result.current.items.map((i) => i.id)).toEqual(["a1", "a2", "n1"]),
    )
  })

  it("update 原地替换文本；remove 移除该条", async () => {
    voicePhraseUpdate.mockResolvedValue({ ok: true, item: { id: "a1", text: "改过的" } })
    voicePhraseDelete.mockResolvedValue({ ok: true })
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    await result.current.update("a1", "改过的")
    await waitFor(() => expect(result.current.items[0].text).toBe("改过的"))

    await result.current.remove("a2")
    await waitFor(() => expect(result.current.items.map((i) => i.id)).toEqual(["a1"]))
    expect(voicePhraseDelete).toHaveBeenCalledWith("a2")
  })
})

describe("usePhraseBoard · 播放", () => {
  it("★ 同一 tick 连点两次只放行一次（ref 锁防双击重复合成）", async () => {
    let release!: () => void
    voicePhraseSpeak.mockImplementation(
      () =>
        new Promise<{ ok: boolean; url: string; duration_s: number; cached: boolean; voice_id: string }>(
          (res) => {
            release = () =>
              res({ ok: true, url: "/api/media/outputs/phrase_cache/x.wav", duration_s: 1, cached: false, voice_id: "wang" })
          },
        ),
    )
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    void result.current.play("a1")
    void result.current.play("a1") // 双击：第二次必须被 ref 挡住

    expect(voicePhraseSpeak).toHaveBeenCalledTimes(1)
    expect(voicePhraseSpeak).toHaveBeenCalledWith("a1", "wang")
    release()
    await waitFor(() => expect(result.current.activeId).toBe("a1"))
  })

  it("★ 合成进行中（请求挂着）CRUD 依然可用 —— 两把锁分开", async () => {
    voicePhraseSpeak.mockImplementation(() => new Promise(() => {})) // 永不返回：模拟冷态合成
    voicePhraseCreate.mockResolvedValue({ ok: true, item: { id: "n1", text: "新短语" } })
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    void result.current.play("a1")
    await result.current.add("新短语") // 若与合成共用一把锁，这里会一直等

    expect(voicePhraseCreate).toHaveBeenCalledTimes(1)
  })

  it("播放失败：错误可见且 activeId 归零", async () => {
    voicePhraseSpeak.mockRejectedValue(new Error("请先选择音色"))
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    await result.current.play("a1")
    await waitFor(() => expect(result.current.error).toContain("请先选择音色"))
    expect(result.current.activeId).toBe("")
  })

  it("stop 掐掉播放：activeId 归零", async () => {
    voicePhraseSpeak.mockResolvedValue({
      ok: true,
      url: "/api/media/outputs/phrase_cache/x.wav",
      duration_s: 1,
      cached: true,
      voice_id: "wang",
    })
    const { result } = renderHook(() => usePhraseBoard())
    await waitFor(() => expect(result.current.items).toHaveLength(2))

    await result.current.play("a2")
    await waitFor(() => expect(result.current.activeId).toBe("a2"))

    act(() => result.current.stop())
    expect(result.current.activeId).toBe("")
  })
})
