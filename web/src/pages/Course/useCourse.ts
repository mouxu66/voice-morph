import { useCallback, useEffect, useRef, useState } from "react"
import {
  cancelCourse,
  getCourseStatus,
  parseCourse,
  retryCourse,
  runCourse,
  type CoursePageIn,
  type CourseStatus,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

const POLL_MS = 2000

/**
 * 讲稿录课的状态与动作（sound.course）。
 *
 * `enabled=false`（能力被关，tab 已藏）时不轮询 —— 后端端点此时不存在，
 * 轮询只会 404 空转（与 useAudiobook 同一契约，index.gate.test.ts 钉着）。
 */
export function useCourse(enabled = true) {
  const { backendUp, voices, selectedVoiceId, selectVoice } = useAppStore()
  const [pages, setPages] = useState<CoursePageIn[]>([])
  const [source, setSource] = useState<"pptx" | "text" | "">("")
  const [skipped, setSkipped] = useState(0)
  const [fileName, setFileName] = useState("")
  const [gapMs, setGapMs] = useState(350)
  const [parsing, setParsing] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [status, setStatus] = useState<CourseStatus | null>(null)
  const pollTimer = useRef<number | null>(null)

  const stopPoll = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const s = await getCourseStatus()
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
        const s = await getCourseStatus()
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

  const running = Boolean(status?.running)
  const canStart =
    backendUp && !running && !submitting && pages.length > 0 && Boolean(selectedVoiceId)

  const loadSource = useCallback(
    async (file: File | undefined, rawText: string) => {
      setParsing(true)
      setErrorMessage("")
      try {
        const r = await parseCourse(file, rawText)
        setPages(r.pages)
        setSource(r.source)
        setSkipped(r.skipped)
        setFileName(file?.name ?? "")
      } catch (error) {
        setErrorMessage(friendlyError(error, "解析失败"))
      } finally {
        setParsing(false)
      }
    },
    []
  )

  const loadFile = useCallback(
    (file: File | undefined) => {
      if (!file) return
      if (file.name.toLowerCase().endsWith(".pptx")) {
        void loadSource(file, "")
      } else {
        void file.text().then((content) => loadSource(undefined, content))
      }
    },
    [loadSource]
  )

  const loadText = useCallback(
    (rawText: string) => {
      void loadSource(undefined, rawText)
    },
    [loadSource]
  )

  const start = useCallback(async () => {
    if (!canStart) return
    setSubmitting(true)
    setErrorMessage("")
    try {
      const r = await runCourse(pages, selectedVoiceId ?? "", gapMs)
      setStatus({
        running: true,
        status: "running",
        voice_id: r.voice_id,
        total: r.total,
        done: 0,
        percent: 0,
        current_page: 0,
        current_text: "",
        job_dir: r.job_dir,
        gap_ms: gapMs,
        pages: [],
        error: "",
      })
      startPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "任务提交失败"))
    } finally {
      setSubmitting(false)
    }
  }, [canStart, pages, selectedVoiceId, gapMs, startPoll])

  const cancel = useCallback(async () => {
    try {
      await cancelCourse()
      setErrorMessage("")
    } catch (error) {
      setErrorMessage(friendlyError(error, "取消失败"))
    }
  }, [])

  const retry = useCallback(async () => {
    setErrorMessage("")
    try {
      const r = await retryCourse()
      setStatus((prev) =>
        prev
          ? { ...prev, running: true, status: "running", total: r.total, done: 0, percent: 0 }
          : prev
      )
      startPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "重试失败"))
    }
  }, [startPoll])

  return {
    backendUp,
    voices,
    selectedVoiceId,
    selectVoice,
    pages,
    source,
    skipped,
    fileName,
    gapMs,
    setGapMs,
    parsing,
    submitting,
    running,
    canStart,
    errorMessage,
    setErrorMessage,
    status,
    loadFile,
    loadText,
    start,
    cancel,
    retry,
  }
}
