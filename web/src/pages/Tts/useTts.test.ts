/**
 * 输字变声的"参考录音"门槛。
 *
 * 背景（2026-10-16 用户实报）：音色列表里有一类"仅换声"音色（RVC 模型音色，
 * 没有 reference.wav）—— 选进输字变声/风格参考必被后端 404（voice_ref 校验），
 * 而前端统一翻译文案「找不到对应的音色档案」会让人以为音色坏了/没了。
 * 这里钉住三层行为：
 *   1. 明确知道选中音色没有参考录音时，canGenerate=false（生成按钮禁用）；
 *   2. generate() 直接拦下并给「合成 → 变声处理」替代路径，不打后端；
 *   3. 有参考录音的音色照常合成 —— 门槛别误伤正常路径。
 */
import { act, renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useAppStore } from "@/store/useAppStore"
import type * as clientModule from "@/api/client"

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof clientModule>("@/api/client")
  return {
    ...actual,
    sendTts: vi.fn(),
    ttsChainCheck: vi.fn(),
    purgeSession: vi.fn(),
    saveSession: vi.fn(),
  }
})

const client = await import("@/api/client")
const sendTts = vi.mocked(client.sendTts)
const ttsChainCheck = vi.mocked(client.ttsChainCheck)

const { useTts } = await import("@/pages/Tts/useTts")

/** 有参考录音的音色（能输字） */
const WITH_REF = {
  id: "kangaroo",
  display_name: "袋鼠骑士",
  reference: "reference.wav",
  duration_s: 6.4,
  has_reference: true,
}
/** 仅换声的模型音色（无参考录音，不能输字） */
const NO_REF = {
  id: "myvoice_supp2026",
  display_name: "myvoice_supp2026",
  reference: "",
  duration_s: 0,
  kind: "rvc_model" as const,
  has_reference: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  useAppStore.setState({
    backendUp: true,
    voices: [WITH_REF, NO_REF],
    selectedVoiceId: "kangaroo",
  })
  ttsChainCheck.mockResolvedValue({ ok: true, all_ok: true, items: [] })
})

describe("useTts · 仅换声音色的输字门槛", () => {
  it("★ 选中无参考录音的音色 → canGenerate=false；换回有参考音色 → 恢复", async () => {
    const { result } = renderHook(() => useTts())
    act(() => { result.current.setText("你好") })
    await waitFor(() => expect(ttsChainCheck).toHaveBeenCalled())

    // 基线：有参考录音，可以生成
    expect(result.current.selectedVoiceNoRef).toBe(false)
    expect(result.current.canGenerate).toBe(true)

    act(() => { useAppStore.setState({ selectedVoiceId: "myvoice_supp2026" }) })
    await waitFor(() => expect(result.current.selectedVoiceNoRef).toBe(true))
    expect(result.current.canGenerate).toBe(false)
  })

  it("generate() 对仅换声音色直接拦下：不打后端，给「变声处理」替代路径", async () => {
    useAppStore.setState({ selectedVoiceId: "myvoice_supp2026" })
    const { result } = renderHook(() => useTts())
    act(() => { result.current.setText("你好") })
    await waitFor(() => expect(ttsChainCheck).toHaveBeenCalled())

    await act(async () => { await result.current.generate() })

    expect(sendTts).not.toHaveBeenCalled()
    expect(result.current.errorMessage).toContain("参考录音")
    expect(result.current.errorMessage).toContain("变声处理")
  })

  it("有参考录音的音色照常合成（门槛别误伤正常路径）", async () => {
    sendTts.mockResolvedValue({
      url: "/api/media/outputs/.session/x.wav",
      duration_s: 1,
      voice_id: "kangaroo",
    })
    const { result } = renderHook(() => useTts())
    act(() => { result.current.setText("你好") })
    await waitFor(() => expect(ttsChainCheck).toHaveBeenCalled())

    await act(async () => { await result.current.generate() })

    expect(sendTts).toHaveBeenCalledTimes(1)
    expect(result.current.ttsHistory.length).toBeGreaterThan(0)
    expect(result.current.errorMessage).toBe("")
  })
})
