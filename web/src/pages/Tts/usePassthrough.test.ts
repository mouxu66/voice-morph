/**
 * 真声直讲 hook 的**状态机**不变量（`usePassthrough.ts`）。
 *
 * 这个 hook 直接决定老师上课时"按一下会发生什么"，所以三条都要钉住：
 *
 * 1. **互斥用 ref 而不是 state**。开关是「开/关」二态，连点两次的后果不是
 *    "多按一下"，而是**发两次 start**（第二次命中 idempotent 返回）或
 *    "start 与 stop 交错"→ 界面与现实不一致。`setBusy` 是异步的，同一事件
 *    循环里读到的还是旧值 → 会穿透。这里钉"同一 tick 连点只发一次请求"。
 *
 * 2. **失败时 `running` 不置 false**。服务端不可达 / 409 互斥 / 设备找不到，
 *    都不代表"它已经停了"——把它置 false 会让界面**谎报已停止**，而声音其实
 *    还在往外送（老师会以为没开、又去点一次）。以 `refresh()` 的返回为准。
 *
 * 3. **停掉时设备名要清空**。停掉还显示"送往 扬声器 (VB-Audio Virtual Cable)"
 *    会让老师以为还在送 —— 而 device 是"声音到底去哪儿"的唯一依据。
 */
import { renderHook, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useAppStore } from "@/store/useAppStore"
import type * as clientModule from "@/api/client"

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof clientModule>("@/api/client")
  return {
    ...actual,
    passthroughStart: vi.fn(),
    passthroughStop: vi.fn(),
    passthroughStatus: vi.fn(),
  }
})

const client = await import("@/api/client")
const passthroughStart = vi.mocked(client.passthroughStart)
const passthroughStop = vi.mocked(client.passthroughStop)
const passthroughStatus = vi.mocked(client.passthroughStatus)

const { usePassthrough } = await import("@/pages/Tts/usePassthrough")

const OFF = { ok: true, running: false, device: "", input_device: "" }
const ON = {
  ok: true,
  running: true,
  device: "扬声器 (VB-Audio Virtual Cable)",
  input_device: "麦克风阵列 (Senary Audio)",
}

beforeEach(() => {
  vi.clearAllMocks()
  useAppStore.setState({ backendUp: true })
  passthroughStatus.mockResolvedValue(OFF)
  passthroughStart.mockResolvedValue({ ok: true, running: true, device: ON.device })
  passthroughStop.mockResolvedValue({ ok: true, running: false })
})

describe("usePassthrough · 开关互斥", () => {
  it("★ 同一 tick 连点两次只发一次 start（state 当锁会穿透）", async () => {
    // 第一次 start 卡住不返回：这正是"还在飞"的现场
    let release!: () => void
    passthroughStart.mockImplementation(
      () =>
        new Promise<{ ok: boolean; running: boolean; device: string }>((res) => {
          release = () => res({ ok: true, running: true, device: ON.device })
        }),
    )

    const { result } = renderHook(() => usePassthrough())
    await waitFor(() => expect(passthroughStatus).toHaveBeenCalled())

    void result.current.toggle()
    void result.current.toggle() // 同一 tick —— 用户双击/连点时的真实时序

    expect(passthroughStart).toHaveBeenCalledTimes(1)

    release()
    await waitFor(() => expect(result.current.busy).toBe(false))
  })

  it("动作跑完之后还能再开关（锁要真释放，不能卡死）", async () => {
    const { result } = renderHook(() => usePassthrough())
    await waitFor(() => expect(passthroughStatus).toHaveBeenCalled())

    // 开启成功后 `toggle` 会再 `await refresh()` 拿 input_device —— 让 status 反映
    // 真实后端（开着时就是 ON），否则 refresh 会把它覆盖回 false。
    passthroughStatus.mockResolvedValue(ON)
    await result.current.toggle() // 开
    await waitFor(() => expect(result.current.running).toBe(true))

    passthroughStatus.mockResolvedValue(OFF)
    await result.current.toggle() // 关
    await waitFor(() => expect(result.current.running).toBe(false))

    expect(passthroughStart).toHaveBeenCalledTimes(1)
    expect(passthroughStop).toHaveBeenCalledTimes(1)
  })
})

describe("usePassthrough · 状态可信度", () => {
  it("★ 启动失败时不许把 running 说成 false（服务端可能还开着）", async () => {
    // 首次 status 说它**正在跑**（比如上个会话留下的）
    passthroughStatus.mockResolvedValue(ON)
    // 用户去点"关闭"，但请求失败（后端重启中）
    passthroughStop.mockRejectedValue(new Error("Failed to fetch"))

    const { result } = renderHook(() => usePassthrough())
    await waitFor(() => expect(result.current.running).toBe(true))

    await result.current.toggle()

    // ⚠️ `error` 是 state：`await toggle()` 只保证**请求**回来了，state 提交与
    // 重渲染还没发生 —— 直接读 `result.current.error` 会拿到旧快照（空串），
    // 看起来像"产品没写错误处理"的假红。
    await waitFor(() => expect(result.current.error).not.toBe(""))
    // 关键：失败后 `refresh()` 拿回的事实仍是 running=true —— 界面必须跟它
    await waitFor(() => expect(result.current.running).toBe(true))
  })

  it("状态被外部改掉时轮询能跟上（互斥被别的模块停掉）", async () => {
    vi.useFakeTimers()
    try {
      passthroughStatus.mockResolvedValue(ON)
      const { result } = renderHook(() => usePassthrough())
      await vi.waitFor(() => expect(result.current.running).toBe(true))

      // 外部（rvc_live 抢占）把直通停了
      passthroughStatus.mockResolvedValue(OFF)
      await vi.advanceTimersByTimeAsync(5100)

      await vi.waitFor(() => expect(result.current.running).toBe(false))
    } finally {
      vi.useRealTimers()
    }
  })

  it("后端不可达时 canUse=false（按钮该禁用，别让用户点了没反应）", async () => {
    useAppStore.setState({ backendUp: false })
    const { result } = renderHook(() => usePassthrough())
    expect(result.current.canUse).toBe(false)
    // 不该去问后端（省一次必失败的请求）
    expect(passthroughStatus).not.toHaveBeenCalled()
  })
})

describe("usePassthrough · 设备名", () => {
  it("★ 关闭后清空设备名 —— 停掉还显示'送往 X'会让老师以为还在送", async () => {
    passthroughStatus.mockResolvedValue(ON)
    const { result } = renderHook(() => usePassthrough())
    await waitFor(() => expect(result.current.device).toBe(ON.device))
    expect(result.current.inputDevice).toBe(ON.input_device)

    await result.current.toggle()

    await waitFor(() => expect(result.current.running).toBe(false))
    expect(result.current.device).toBe("")
    expect(result.current.inputDevice).toBe("")
  })

  it("开启后能拿到两个设备名（界面靠它们说明'声音往哪送、从哪来'）", async () => {
    const { result } = renderHook(() => usePassthrough())
    await waitFor(() => expect(passthroughStatus).toHaveBeenCalled())

    passthroughStatus.mockResolvedValue(ON)
    await result.current.toggle()

    await waitFor(() => expect(result.current.device).toBe(ON.device))
    expect(result.current.inputDevice).toBe(ON.input_device)
  })
})
