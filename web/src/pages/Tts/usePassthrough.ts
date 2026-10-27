import { useCallback, useEffect, useRef, useState } from "react"

import { passthroughStart, passthroughStatus, passthroughStop } from "@/api/client"
import { friendlyError } from "@/lib/errors"
import { useAppStore } from "@/store/useAppStore"

/**
 * 真声直讲：把麦克风的**真声原样**灌进虚拟声卡，让对面（QQ/微信/钉钉）听到
 * 老师本人在讲课 —— 同时热键插的 AI 音会**叠加**上去，不打断真声。
 *
 * 背景（`docs/交接-AI必读.md`）：现有三条链路都不满足"真声讲课 + 偶发插话"：
 *   实时变声  → 真声被**替换**成变声音色
 *   级联      → 同样替换，还要等 1.5~2s
 *   热键      → 只播那一下，真声不在里面
 * 所以需要这条通路。
 *
 * ⚠️ 界面必须提示用户在**外部软件**里做的事：把 QQ/微信的**麦克风**设为
 * `CABLE Output (VB-Audio Virtual Cable)`。少了这步，用户会以为功能坏了
 * —— 因为声音明明进了虚拟声卡，但对面听的是物理麦克风。
 */
export function usePassthrough() {
  const { backendUp } = useAppStore()
  const [running, setRunning] = useState(false)
  /** 声音正送往哪（CABLE 渲染端）—— 让用户确认"确实送出去了" */
  const [device, setDevice] = useState("")
  /** 采集来源（真麦）—— 插了耳机/换了麦之后能看出来跟没跟上 */
  const [inputDevice, setInputDevice] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  /**
   * 开关互斥用 **ref**：`useState` 是异步的，同一事件循环里连点两次读到的还是
   * 旧值 → 会发出两次 start/stop（口径与 `usePhraseBoard.busyRef` 一致）。
   */
  const busyRef = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const s = await passthroughStatus()
      setRunning(s.running)
      setDevice(s.device)
      setInputDevice(s.input_device)
    } catch {
      /* 服务离线时静默：下次轮询/操作再试。不把 running 置 false —— 那会让
         界面在"后端短暂不可达"时谎报"已停止"，比不更新更危险。 */
    }
  }, [])

  useEffect(() => {
    if (!backendUp) return
    void refresh()
    // 状态可能被**外部**改变（互斥时被另一个模块停掉、后端重启），所以轮询。
    // 5s 与 App.tsx 的 health 轮询同频，不额外加负载。
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [backendUp, refresh])

  const toggle = useCallback(async () => {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    setError("")
    try {
      if (running) {
        await passthroughStop()
        setRunning(false)
        setDevice("")
        setInputDevice("")
      } else {
        const r = await passthroughStart()
        setRunning(r.running)
        setDevice(r.device)
        await refresh() // 拿 input_device（start 的返回里没有）
      }
    } catch (e) {
      setError(friendlyError(e))
      await refresh() // 失败后以服务端实际状态为准（409 互斥时界面不该停在"已开"）
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }, [running, refresh])

  return {
    running,
    device,
    inputDevice,
    busy,
    error,
    toggle,
    /** 后端不可达时禁用按钮（与其它面板同口径） */
    canUse: backendUp,
  }
}
