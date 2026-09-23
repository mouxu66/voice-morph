import { useCallback, useEffect, useState } from "react"
import {
  getWarmup,
  getWechatHistory,
  getWechatLastTts,
  wechatManualSend,
  wechatPlayToCable,
  wechatSendVoice,
  type WarmupStatus,
  type WechatHistoryItem,
  type WechatLastTts,
  type WechatSendResult,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

/**
 * 微信语音发送：三档路径共用一个 hook。
 * 全自动（模拟 Alt + 播放）/ 半自动（播放到虚拟声卡，人手按 Alt）/ 手动实时变声。
 *
 * 发送目标（`targetWav`）默认是"最近一次合成产物"，但**预混结果可以顶替它**：
 * 声板的预混模式把勾选的音效离线混进一份新音频（`sfxmix_*.wav`），用户点发送时该发的是那份。
 * 这里记下预混的**源**，源一变（重新合成了一条）就自动失效 —— 否则会出现
 * "再合成一条新语音、点发送却发出上次混过的旧内容"，而且没有任何报错。
 *
 * `enabled=false`（hook.wechat 能力被关，tab 已藏）时不轮询 —— 后端端点此时不存在，
 * 轮询只会 404 空转。
 */
export function useWechatSend(enabled = true) {
  const { backendUp } = useAppStore()
  const [lastTts, setLastTts] = useState<WechatLastTts | null>(null)
  const [history, setHistory] = useState<WechatHistoryItem[]>([])
  const [busy, setBusy] = useState<"" | "send" | "play" | "manual">("")
  const [errorMessage, setErrorMessage] = useState("")
  const [lastResult, setLastResult] = useState<WechatSendResult | null>(null)
  const [warmup, setWarmup] = useState<WarmupStatus | null>(null)

  // 轮询后端预热：起来后每 2s 查一次直到 done。预热未完成就发语音，请求会阻塞在
  // 模型加载上（安全，但第一次会等几十秒）——这里把进度暴露给界面，别让用户以为卡死。
  useEffect(() => {
    if (!enabled || !backendUp) {
      setWarmup(null)
      return
    }
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      try {
        const w = await getWarmup()
        if (!alive) return
        setWarmup(w)
        if (!w.done) timer = setTimeout(() => void tick(), 2000)
      } catch {
        if (alive) timer = setTimeout(() => void tick(), 3000)
      }
    }
    void tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [enabled, backendUp])

  // 声板预混的产物（`sound.fx-board` 面板产出）。
  const [premix, setPremix] = useState<{
    source: string
    wav: string
    inserts: number
    seconds: number
  } | null>(null)
  const sourceWav = lastTts?.wav
  const premixActive = Boolean(premix && sourceWav && premix.source === sourceWav)
  const targetWav = premixActive && premix ? premix.wav : sourceWav
  const clearPremix = useCallback(() => setPremix(null), [])

  const refresh = useCallback(async () => {
    try {
      const [last, hist] = await Promise.all([getWechatLastTts(), getWechatHistory()])
      setLastTts(last)
      setHistory(hist.items ?? [])
    } catch {
      /* 服务离线时静默，下次操作再试 */
    }
  }, [])

  useEffect(() => {
    if (enabled && backendUp) void refresh()
  }, [enabled, backendUp, refresh])

  const run = useCallback(
    async (kind: "send" | "play" | "manual", fn: () => Promise<WechatSendResult>) => {
      if (busy) return
      setBusy(kind)
      setErrorMessage("")
      setLastResult(null)
      try {
        const r = await fn()
        setLastResult(r)
        if (!r.ok && r.error) setErrorMessage(r.error)
      } catch (error) {
        setErrorMessage(friendlyError(error, "操作失败"))
      } finally {
        setBusy("")
        void refresh()
      }
    },
    [busy, refresh],
  )

  // 不传 wav 时用 `targetWav`（预混结果优先）—— 注意**不能**把 undefined 透给后端：
  // 后端把"没给 wav"理解成"取最近一条 tts_*.wav"，那会绕过预混结果。
  const sendAuto = useCallback(
    (wav?: string) => run("send", () => wechatSendVoice(wav ?? targetWav)),
    [run, targetWav],
  )
  const playToCable = useCallback(
    (wav?: string) => run("play", () => wechatPlayToCable(wav ?? targetWav)),
    [run, targetWav],
  )
  const manualSetup = useCallback(() => run("manual", () => wechatManualSend()), [run])

  return {
    backendUp,
    lastTts,
    history,
    busy,
    errorMessage,
    lastResult,
    warmup,
    warming: Boolean(warmup && !warmup.done),
    sendAuto,
    playToCable,
    manualSetup,
    refresh,
    premix,
    premixActive,
    targetWav,
    setPremix,
    clearPremix,
  }
}
