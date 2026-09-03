import { useCallback, useEffect, useState } from "react"
import {
  getWechatHistory,
  getWechatLastTts,
  wechatManualSend,
  wechatPlayToCable,
  wechatSendVoice,
  type WechatHistoryItem,
  type WechatLastTts,
  type WechatSendResult,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

/**
 * 微信语音发送：三档路径共用一个 hook。
 * 全自动（模拟 Alt + 播放）/ 半自动（播放到虚拟声卡，人手按 Alt）/ 手动实时变声。
 */
export function useWechatSend() {
  const { backendUp } = useAppStore()
  const [lastTts, setLastTts] = useState<WechatLastTts | null>(null)
  const [history, setHistory] = useState<WechatHistoryItem[]>([])
  const [busy, setBusy] = useState<"" | "send" | "play" | "manual">("")
  const [errorMessage, setErrorMessage] = useState("")
  const [lastResult, setLastResult] = useState<WechatSendResult | null>(null)

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
    if (backendUp) void refresh()
  }, [backendUp, refresh])

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

  const sendAuto = useCallback((wav?: string) => run("send", () => wechatSendVoice(wav)), [run])
  const playToCable = useCallback((wav?: string) => run("play", () => wechatPlayToCable(wav)), [run])
  const manualSetup = useCallback(() => run("manual", () => wechatManualSend()), [run])

  return {
    backendUp,
    lastTts,
    history,
    busy,
    errorMessage,
    lastResult,
    sendAuto,
    playToCable,
    manualSetup,
    refresh,
  }
}
