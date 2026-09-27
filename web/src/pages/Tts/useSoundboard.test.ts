/**
 * 声板 hook 的两处**状态机**不变量。
 *
 * 1. **素材动作真互斥**（`withBusy`）。这些动作都会改素材目录，而目录是格子面板的
 *    渲染依据；两个同时在飞时，后完成的那次刷新可能先落库 —— 界面停在一个
 *    "少一条"的状态上，刷新一次又对了（典型偶发错）。
 *    原来的实现拿 **state** 当锁：`setBusy` 是异步的，同一个事件循环里连点两次
 *    读到的是同一个旧值，两次都会放行 —— 也就是**同帧连点能穿透**。
 *    这里钉的是"同一 tick 连续两次调用只放行一次"。
 *
 * 2. **货架失败可重试**。原来失败也置 `loaded=true`，于是本次会话再点开货架
 *    只会看到上一次那句错误，唯一的出路是重进页面 —— 而这类失败的常见成因
 *    （后端刚起来 / 网络抖一下）本来重试就好。
 */
import { renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useAppStore } from "@/store/useAppStore"

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client")
  return {
    ...actual,
    soundboardCatalog: vi.fn(),
    soundboardPacks: vi.fn(),
    soundboardPacksAvailable: vi.fn(),
    soundboardWarm: vi.fn(),
    soundboardImport: vi.fn(),
  }
})

const client = await import("@/api/client")
const soundboardCatalog = vi.mocked(client.soundboardCatalog)
const soundboardPacks = vi.mocked(client.soundboardPacks)
const soundboardPacksAvailable = vi.mocked(client.soundboardPacksAvailable)
const soundboardWarm = vi.mocked(client.soundboardWarm)
const soundboardImport = vi.mocked(client.soundboardImport)

const { useSoundboard } = await import("@/pages/Tts/useSoundboard")

beforeEach(() => {
  vi.clearAllMocks()
  useAppStore.setState({ backendUp: true })
  soundboardCatalog.mockResolvedValue({ ok: true, items: [] })
  soundboardPacks.mockResolvedValue({ ok: true, packs: [], samples: 0 })
  soundboardWarm.mockResolvedValue({ ok: true, ready: true, samples: 0 })
})

describe("useSoundboard · 素材动作互斥", () => {
  it("★ 同一 tick 连点两次只放行一次（state 当锁会穿透）", async () => {
    // 第一次导入卡住不返回：这正是"还在飞"的现场
    let release!: () => void
    soundboardImport.mockImplementation(
      () =>
        new Promise<{ ok: boolean; id: string; duration_s: number }>((res) => {
          release = () => res({ ok: true, id: "a", duration_s: 0.1 })
        }),
    )

    const { result } = renderHook(() => useSoundboard())
    await waitFor(() => expect(soundboardCatalog).toHaveBeenCalled())

    const file = new File([new Uint8Array([1])], "a.wav", { type: "audio/wav" })
    // 同一 tick 里连着两次 —— 用户双击/连点时的真实时序
    void result.current.importFiles([file])
    void result.current.importFiles([file])

    // 第二次必须被挡住。用 state 判互斥时这里是 2（`setBusy` 还没触发重渲染，
    // 第二次读到的 `busy` 仍是空串）—— 两次导入会真的并发跑到后端。
    expect(soundboardImport).toHaveBeenCalledTimes(1)

    release()
    await waitFor(() => expect(result.current.busy).toBe(""))
  })

  it("动作跑完之后还能再发起下一次（锁要真的释放，不能卡死）", async () => {
    soundboardImport.mockResolvedValue({ ok: true, id: "a", duration_s: 0.1 })
    const { result } = renderHook(() => useSoundboard())
    await waitFor(() => expect(soundboardCatalog).toHaveBeenCalled())

    const file = new File([new Uint8Array([1])], "a.wav", { type: "audio/wav" })
    await result.current.importFiles([file])
    await result.current.importFiles([file])
    expect(soundboardImport).toHaveBeenCalledTimes(2)
  })
})

describe("useSoundboard · 货架", () => {
  it("★ 加载失败后允许重试，而不是本次会话只能看旧错误", async () => {
    soundboardPacksAvailable.mockRejectedValueOnce(new Error("后端刚起来"))
    const { result } = renderHook(() => useSoundboard())
    await waitFor(() => expect(soundboardCatalog).toHaveBeenCalled())

    await result.current.openShelf()
    // ⚠️ `shelf` 是 state：`await openShelf()` 只保证**请求**回来了，
    // state 提交与重渲染还没发生，此刻读 `result.current.shelf` 是旧快照（空串）。
    // 必须 `waitFor` 等它落地 —— 直接断言会得到一个"看起来像产品没写"的假红。
    await waitFor(() => expect(result.current.shelf.error).toContain("后端刚起来"))

    // 再点开货架：必须**重新请求**（后端已经起来了，这次能拿到清单）
    soundboardPacksAvailable.mockResolvedValueOnce({
      ok: true,
      source: "https://example.test/packs.json",
      items: [{ id: "p1", name: "萌宠包", count: 3, author: "", license: "CC0" }],
      error: "",
      note: "",
    } as never)
    await result.current.openShelf()

    expect(soundboardPacksAvailable).toHaveBeenCalledTimes(2)
    await waitFor(() =>
      expect(result.current.shelf.items.map((p) => p.id)).toEqual(["p1"]),
    )
    expect(result.current.shelf.error).toBe("")
  })

  it("成功之后不重复请求（懒加载的「不白碰一次网络」要保住）", async () => {
    soundboardPacksAvailable.mockResolvedValue({
      ok: true, source: "s", items: [], error: "", note: "",
    } as never)
    const { result } = renderHook(() => useSoundboard())
    await waitFor(() => expect(soundboardCatalog).toHaveBeenCalled())

    await result.current.openShelf()
    await result.current.openShelf()
    expect(soundboardPacksAvailable).toHaveBeenCalledTimes(1)
  })

  it("后端**明确答复**清单不可用时不算失败（重试也拿不到，不该每次点开都重问）", async () => {
    soundboardPacksAvailable.mockResolvedValue({
      ok: true, source: null, items: [], error: "没有配置清单地址", note: "",
    } as never)
    const { result } = renderHook(() => useSoundboard())
    await waitFor(() => expect(soundboardCatalog).toHaveBeenCalled())

    await result.current.openShelf()
    await result.current.openShelf()
    expect(soundboardPacksAvailable).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(result.current.shelf.error).toContain("没有配置"))
  })
})
