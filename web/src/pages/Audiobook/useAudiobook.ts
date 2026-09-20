import { useCallback, useEffect, useRef, useState } from "react"
import {
  cancelAudiobook,
  getAudiobookStatus,
  runAudiobook,
  type AudiobookStatus,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

const POLL_MS = 2000

/**
 * `enabled=false`（sound.audiobook 能力被关，tab 已藏）时不轮询 ——
 * 后端端点此时不存在，轮询只会 404 空转。
 */
export function useAudiobook(enabled = true) {
  const { backendUp, voices, selectedVoiceId, selectVoice } = useAppStore()
  const [text, setText] = useState("")
  const [gapMs, setGapMs] = useState(350)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [status, setStatus] = useState<AudiobookStatus | null>(null)
  const pollTimer = useRef<number | null>(null)

  const stopPoll = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const s = await getAudiobookStatus()
      setStatus(s)
      if (!s.running) stopPoll()
    } catch {
      /* 服务离线时静默，下一次轮询再试 */
    }
  }, [stopPoll])

  // 挂载时拉一次状态（服务端任务可能仍在跑），running 则开始轮询
  useEffect(() => {
    if (!enabled) return
    void (async () => {
      try {
        const s = await getAudiobookStatus()
        setStatus(s)
        if (s.running) {
          stopPoll()
          pollTimer.current = window.setInterval(() => void refresh(), POLL_MS)
        }
      } catch {
        /* ignore */
      }
    })()
    return stopPoll
  }, [enabled, refresh, stopPoll])

  const startPoll = useCallback(() => {
    stopPoll()
    pollTimer.current = window.setInterval(() => void refresh(), POLL_MS)
  }, [refresh, stopPoll])

  const isSrt = text.includes("-->")
  const running = Boolean(status?.running)
  const canStart =
    backendUp && !running && !submitting && text.trim().length > 0 && Boolean(selectedVoiceId)

  const start = useCallback(async () => {
    if (!canStart) return
    setSubmitting(true)
    setErrorMessage("")
    try {
      const r = await runAudiobook(text, selectedVoiceId ?? "", gapMs)
      setStatus({
        running: true,
        status: "running",
        mode: r.mode as AudiobookStatus["mode"],
        voice_id: r.voice_id,
        total: r.total,
        done: 0,
        percent: 0,
        current_text: "",
        url: "",
        duration_s: 0,
        error: "",
        segments: [],
      })
      startPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "任务提交失败"))
    } finally {
      setSubmitting(false)
    }
  }, [canStart, text, selectedVoiceId, gapMs, startPoll])

  const cancel = useCallback(async () => {
    try {
      await cancelAudiobook()
      setErrorMessage("")
    } catch (error) {
      setErrorMessage(friendlyError(error, "取消失败"))
    }
  }, [])

  const loadFile = useCallback((file: File | undefined) => {
    if (!file) return
    void file.text().then((content) => {
      setText(content)
      setErrorMessage("")
    })
  }, [])

  return {
    backendUp,
    voices,
    selectedVoiceId,
    selectVoice,
    text,
    setText,
    loadFile,
    isSrt,
    gapMs,
    setGapMs,
    submitting,
    running,
    canStart,
    errorMessage,
    setErrorMessage,
    status,
    start,
    cancel,
  }
}
