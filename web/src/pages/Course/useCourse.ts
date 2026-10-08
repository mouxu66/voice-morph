import { useCallback, useEffect, useRef, useState } from "react"
import {
  cancelCourse,
  cancelCourseVideo,
  getCourseStatus,
  getCourseVideoStatus,
  parseCourse,
  retryCourse,
  runCourse,
  runCourseVideo,
  type CoursePageIn,
  type CourseStatus,
  type CourseVideoStatus,
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
  /**
   * 风格参考 ICL：""=不启用（纯声纹克隆，语调平）；非空=用该音色的参考音做整段
   * 克隆（有情绪版旁白，见 client.ts 的 `CourseStatus.style_ref_voice` 注释）。
   */
  const [styleRefVoice, setStyleRefVoice] = useState("")
  const [parsing, setParsing] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [status, setStatus] = useState<CourseStatus | null>(null)
  /**
   * 上传课件的暂存裸名（后端 parse 时存进会话目录）。合成时要回传 ——
   * 成片的画面就是从这个文件导的；贴文本来的批次是空串（出不了画面）。
   */
  const [sourcePptx, setSourcePptx] = useState("")
  const [video, setVideo] = useState<CourseVideoStatus | null>(null)
  const [videoFps, setVideoFps] = useState(30)
  /** 页间停顿（秒*1000）。默认跟随合成那个，可单独调 —— 成片的节奏由它定。 */
  const [videoGapMs, setVideoGapMs] = useState(350)
  const [forceSlides, setForceSlides] = useState(false)
  const pollTimer = useRef<number | null>(null)
  const videoTimer = useRef<number | null>(null)

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

  // ---- 导出成片：自己的轮询（与逐页合成互不阻塞，两个后台任务各跑各的）----

  const stopVideoPoll = useCallback(() => {
    if (videoTimer.current != null) {
      window.clearInterval(videoTimer.current)
      videoTimer.current = null
    }
  }, [])

  const refreshVideo = useCallback(async () => {
    try {
      const v = await getCourseVideoStatus()
      setVideo(v)
      if (!v.running) stopVideoPoll()
    } catch {
      /* 服务离线时静默，下一次轮询再试 */
    }
  }, [stopVideoPoll])

  const startVideoPoll = useCallback(() => {
    stopVideoPoll()
    videoTimer.current = window.setInterval(() => void refreshVideo(), POLL_MS)
  }, [refreshVideo, stopVideoPoll])

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
      try {
        const v = await getCourseVideoStatus()
        setVideo(v)
        if (v.running) {
          stopVideoPoll()
          videoTimer.current = window.setInterval(() => void refreshVideo(), POLL_MS)
        }
      } catch {
        /* ignore */
      }
    })()
    return () => {
      stopPoll()
      stopVideoPoll()
    }
  }, [enabled, refresh, refreshVideo, stopPoll, stopVideoPoll])

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
        setSourcePptx(r.source_pptx ?? "")
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
        setSourcePptx("")
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
      const r = await runCourse(pages, selectedVoiceId ?? "", gapMs, styleRefVoice, sourcePptx)
      setStatus({
        running: true,
        status: "running",
        voice_id: r.voice_id,
        style_ref_voice: styleRefVoice,
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
      // 新一批合成 → 上一次的成片不再代表眼前这批，先清掉（免得用户以为它是新的）
      setVideo(null)
      startPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "任务提交失败"))
    } finally {
      setSubmitting(false)
    }
  }, [canStart, pages, selectedVoiceId, gapMs, styleRefVoice, sourcePptx, startPoll])

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

  // ---- 导出成片 ----

  const videoRunning = Boolean(video?.running)
  /**
   * 能导出成片 = 有一批合成好的页 + 这一批带着源课件 + 两个任务都没在跑。
   * 贴文本来的批次没有源课件 → 按钮禁用并由页面解释原因（不做一个点了必 400 的按钮）。
   */
  const canExportVideo =
    backendUp && !running && !videoRunning && Boolean(sourcePptx) && Boolean(status?.pages.some((pg) => !pg.failed && pg.url))

  const startVideo = useCallback(async () => {
    if (!canExportVideo) return
    setErrorMessage("")
    try {
      await runCourseVideo({
        fps: videoFps,
        gapMs: videoGapMs,
        forceSlides,
        // pin 住批次：导出的一定是右栏这一批，而不是"内存里最后一次"
        jobDir: status?.job_dir ?? "",
      })
      setVideo({
        running: true,
        status: "running",
        step: "",
        percent: 0,
        message: "准备中…",
        job_dir: status?.job_dir ?? "",
        out_url: "",
        out_name: "",
        duration_s: 0,
        pages: status?.pages.length ?? 0,
        slides: 0,
        fps: videoFps,
        gap_ms: videoGapMs,
        error: "",
      })
      startVideoPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "导出成片失败"))
    }
  }, [canExportVideo, forceSlides, status, startVideoPoll, videoFps, videoGapMs])

  const cancelVideo = useCallback(async () => {
    try {
      await cancelCourseVideo()
      setErrorMessage("")
    } catch (error) {
      setErrorMessage(friendlyError(error, "取消导出失败"))
    }
  }, [])

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
    styleRefVoice,
    setStyleRefVoice,
    sourcePptx,
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
    video,
    videoRunning,
    canExportVideo,
    startVideo,
    cancelVideo,
    videoFps,
    setVideoFps,
    videoGapMs,
    setVideoGapMs,
    forceSlides,
    setForceSlides,
  }
}
