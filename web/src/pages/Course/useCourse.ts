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
  /**
   * 讲稿文本已改动但没重新解析 —— `pages` 还是上一轮的旧解析结果。
   *
   * 为什么必须显式失效：合成时提交的是 `pages`，而用户眼前是刚编辑的文本框。
   * 两者不同源却不给任何提示（`derived-state-integrity` 铁律踩过的坑），
   * 结果是"录出来的旁白不是我写的稿"。所以 dirty 时直接禁掉合成按钮，
   * 逼用户按「解析这段文本」把新稿变成页。
   */
  const [dirty, setDirty] = useState(false)
  /**
   * 讲稿原文。**放在 hook 里而不是页面本地 state** —— 导入 .txt 时要把读到的
   * 文本回填进输入框，两处各存一份就会出现"框里空的但能合成"（显示与数据不同源）。
   */
  const [rawText, setRawTextState] = useState("")
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
    backendUp && !running && !submitting && !dirty && pages.length > 0 && Boolean(selectedVoiceId)

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
        setDirty(false)
        // 让文本框与 pages 同源：pptx 成功 → 清掉旧草稿（不然框里是 A、合成的是 B）；
        // txt 成功 → 把读到的原文回填（不然框是空的、页面看着像没东西）。
        if (file) setRawTextState("")
        else if (rawText.trim()) setRawTextState(rawText)
      } catch (error) {
        // ★ 解析失败**清空** pages：留着上一次的解析结果，用户会以为新文件已就位
        // （按钮可点 → 合成的是上一份 pptx 的页）。
        setPages([])
        setSource("")
        setSkipped(0)
        setFileName("")
        setErrorMessage(friendlyError(error, "解析失败"))
      } finally {
        setParsing(false)
      }
    },
    []
  )

  const setRawText = useCallback((v: string) => {
    setRawTextState(v)
    setDirty(true)
  }, [])

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
    dirty,
    rawText,
    setRawText,
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
