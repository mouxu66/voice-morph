import { useCallback, useEffect, useRef, useState } from "react"
import { useSearchParams } from "react-router-dom"
import {
  getOfflineVcStatus,
  listRvcVoices,
  mediaUrl,
  runOfflineVc,
  suggestPitch,
  type OfflineVcStatus,
  type PitchSuggestion,
  type RvcVoice,
} from "@/api/client"
import { friendlyError } from "@/lib/errors"
import { takeOvcHandoff } from "@/lib/ovcHandoff"
import { addPreset, loadPresets, removePreset, type VcPreset } from "@/lib/vcPresets"

const POLL_MS = 2000

export type OvcQueueItem = {
  id: string
  name: string
  size: number
  file: File
  status: "pending" | "running" | "done" | "error"
  url?: string
  durationS?: number
  error?: string
}

export function useOfflineVc() {
  const [rvcVoices, setRvcVoices] = useState<RvcVoice[]>([])
  /** 音色清单**是否已从后端读到过**（与"列表为空"是两件事，见加载 effect 里的注释）。 */
  const [voicesLoaded, setVoicesLoaded] = useState(false)
  const [voiceId, setVoiceId] = useState("")
  const [recording, setRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [audioFile, setAudioFile] = useState<File | null>(null)
  const [pitch, setPitch] = useState(0)
  const [indexRate, setIndexRate] = useState(0.5)
  const [denoise, setDenoise] = useState(true)
  const [enhanceLevel, setEnhanceLevel] = useState("standard")
  const [postSeedVc, setPostSeedVc] = useState(false)
  const [prosody, setProsody] = useState<"keep" | "relay">("keep")
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [status, setStatus] = useState<OfflineVcStatus | null>(null)
  const [queue, setQueue] = useState<OvcQueueItem[]>([])
  const [batchProcessing, setBatchProcessing] = useState(false)
  const [presets, setPresets] = useState<VcPreset[]>(() => loadPresets())
  const pollTimer = useRef<number | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const chunks = useRef<Blob[]>([])
  const tickTimer = useRef<number | null>(null)
  const queueRef = useRef<OvcQueueItem[]>([])
  useEffect(() => { queueRef.current = queue }, [queue])

  // 音色清单（只保留已训练出模型的）+ 恢复进行中任务
  useEffect(() => {
    void (async () => {
      try {
        const r = await listRvcVoices()
        const ready = r.voices.filter((v) => v.model_ready)
        setRvcVoices(ready)
        // ★「加载完成」必须与「列表为空」分开报：深链预选要靠它决定"该不该判定为没找到"。
        // 只用 `rvcVoices.length` 判断会把「服务没起（列表恒空）」误当成「音色不存在」，
        // 于是给用户一句"你带的音色没就绪"的假警报 —— 服务起来后它其实是好的。
        setVoicesLoaded(true)
        setVoiceId((cur) => (ready.some((v) => v.id === cur) ? cur : (ready[0]?.id ?? "")))
      } catch {
        /* 服务未起，页面有离线提示 */
      }
      try {
        const s = await getOfflineVcStatus()
        setStatus(s)
        if (s.running) startPoll()
      } catch {
        /* ignore */
      }
    })()
    return stopPoll
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const stopPoll = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const s = await getOfflineVcStatus()
      setStatus(s)
      if (!s.running) stopPoll()
    } catch {
      /* 下轮再试 */
    }
  }, [stopPoll])

  const startPoll = useCallback(() => {
    stopPoll()
    pollTimer.current = window.setInterval(() => void refresh(), POLL_MS)
  }, [refresh, stopPoll])

  // ---- 深链预选音色：`?voice=<id>`（首页「录一句听听」带过来）----
  //
  // 为什么首页那条入口不直接跳「实时变声」而是要来这儿：实时变声要先有虚拟声卡、
  // 开着麦，用户还很难判断"到底变成没变成"。而「录一句 → 出结果」是**门槛最低、
  // 反馈最直接**的一条 —— 这正是第一次体验该走的路（能力早就有，缺的是入口）。
  //
  // 模式与 `useLive` 的 `?voice=<exp>` 故意保持一致：只 apply 一次（`appliedVoiceParam`
  // 挡住重渲染），且**只在清单里真有这个音色时**才生效，绝不凭空选中一个不存在的。
  const [searchParams] = useSearchParams()
  const voiceParam = searchParams.get("voice")
  const appliedVoiceParam = useRef<string | null>(null)
  /** 深链落地后的引导条：只在**成功**预选过一次时出现，可手动关掉。 */
  const [deepLinkGuide, setDeepLinkGuide] = useState(false)
  /** 带了音色、但它没就绪（没装完 / 缺 index）→ 页面要说实话，而不是假装选中了。 */
  const [deepLinkMiss, setDeepLinkMiss] = useState(false)
  const dismissDeepLinkGuide = useCallback(() => setDeepLinkGuide(false), [])

  useEffect(() => {
    if (!voiceParam || appliedVoiceParam.current === voiceParam) return
    if (!voicesLoaded) return // 还没读到清单：等下一轮，别急着判"没找到"
    appliedVoiceParam.current = voiceParam
    if (rvcVoices.some((v) => v.id === voiceParam)) {
      setVoiceId(voiceParam)
      setDeepLinkGuide(true)
    } else {
      // 首页那条路是"装好才跳"，所以正常到不了这里；真到了（例：装完缺 index、
      // 或用户在别处手改了 URL）就必须明说，否则他会盯着一个不是自己选的音色发懵。
      setDeepLinkMiss(true)
    }
  }, [voiceParam, voicesLoaded, rvcVoices])

  // ---- 录音 ----
  const startRecording = useCallback(async () => {
    setErrorMessage("")
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
        ? "audio/webm;codecs=opus"
        : "audio/webm"
      const rec = new MediaRecorder(stream, { mimeType: mime })
      chunks.current = []
      rec.ondataavailable = (e) => e.data.size && chunks.current.push(e.data)
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop())
        const ext = mime.includes("webm") ? "webm" : "mp3"
        const blob = new Blob(chunks.current, { type: mime })
        setAudioFile(new File([blob], `recording.${ext}`, { type: mime }))
      }
      rec.start()
      recorder.current = rec
      setRecording(true)
      setRecordSeconds(0)
      tickTimer.current = window.setInterval(
        () => setRecordSeconds((s) => s + 1),
        1000
      )
    } catch (error) {
      setErrorMessage(friendlyError(error, "无法访问麦克风，请检查系统权限"))
    }
  }, [])

  const stopRecording = useCallback(() => {
    recorder.current?.stop()
    recorder.current = null
    setRecording(false)
    if (tickTimer.current != null) {
      window.clearInterval(tickTimer.current)
      tickTimer.current = null
    }
  }, [])

  const pickFile = useCallback((file: File | undefined) => {
    if (!file) return
    stopRecording()
    setAudioFile(file)
    setErrorMessage("")
  }, [stopRecording])

  const clearFile = useCallback(() => {
    setAudioFile(null)
    setPitchAdvice(null)
    adviceKeyRef.current = ""
  }, [])

  // ---- 参数预设 ----
  const savePreset = useCallback((name: string) => {
    const trimmed = name.trim()
    if (!trimmed) return
    setPresets(addPreset({ name: trimmed, voiceId, pitch, indexRate, denoise }))
  }, [voiceId, pitch, indexRate, denoise])

  const applyPreset = useCallback((id: string) => {
    const p = loadPresets().find((x) => x.id === id)
    if (!p) return
    setVoiceId(p.voiceId)
    setPitch(p.pitch)
    setIndexRate(p.indexRate)
    setDenoise(p.denoise)
  }, [])
  // 预设不带 enhanceLevel（旧预设无此字段），保持当前选择不被覆盖

  const deletePreset = useCallback((id: string) => {
    setPresets(removePreset(id))
  }, [])

  // ---- 自动音高建议：音频+音色就绪后分析 f0，自动填一次建议变调 ----
  const [pitchAdvice, setPitchAdvice] = useState<PitchSuggestion | null>(null)
  const [pitchBusy, setPitchBusy] = useState(false)
  const adviceKeyRef = useRef("")   // 已自动应用过的 file+voice 组合，之后用户手动调不覆盖

  useEffect(() => {
    if (!audioFile || !voiceId) {
      setPitchAdvice(null)
      return
    }
    const key = `${audioFile.name}|${audioFile.size}|${voiceId}`
    if (key === adviceKeyRef.current) return
    let cancelled = false
    setPitchBusy(true)
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const s = await suggestPitch(audioFile, voiceId)
          if (cancelled) return
          setPitchAdvice(s)
          adviceKeyRef.current = key
          if (s.reliable && s.suggested_pitch != null) setPitch(s.suggested_pitch)
        } catch {
          if (!cancelled) setPitchAdvice(null)
        } finally {
          if (!cancelled) setPitchBusy(false)
        }
      })()
    }, 400)  // 轻防抖：连续换文件/音色时不重复请求
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      setPitchBusy(false)
    }
  }, [audioFile, voiceId])

  const running = Boolean(status?.running)

  // ---- 批量队列 ----
  const updateItem = useCallback((id: string, patch: Partial<OvcQueueItem>) => {
    setQueue((q) => q.map((it) => (it.id === id ? { ...it, ...patch } : it)))
  }, [])

  const importFiles = useCallback((files: File[] | undefined) => {
    if (!files?.length) return
    setQueue((q) => [
      ...q,
      ...files.map((f, i) => ({
        id: `${Date.now()}-${i}-${f.name}`,
        name: f.name,
        size: f.size,
        file: f,
        status: "pending" as const,
      })),
    ])
    setErrorMessage("")
  }, [])

  const removeQueueItem = useCallback((id: string) => {
    setQueue((q) => q.filter((it) => it.id !== id))
  }, [])

  // 接收 TTS 页传来的音频（「变声处理 →」跳转），自动加入批量队列
  const [handoffDone, setHandoffDone] = useState(false)
  useEffect(() => {
    if (handoffDone) return
    const files = takeOvcHandoff()
    if (files.length) importFiles(files)
    setHandoffDone(true)
  }, [handoffDone, importFiles])

  const clearQueue = useCallback(() => {
    setQueue((q) => q.filter((it) => it.status === "running"))
  }, [])

  // 轮询直到当前任务结束，返回最终状态
  const waitDone = useCallback(async (): Promise<OfflineVcStatus | null> => {
    for (;;) {
      await new Promise((r) => setTimeout(r, POLL_MS))
      try {
        const s = await getOfflineVcStatus()
        setStatus(s)
        if (!s.running) return s
      } catch {
        /* 下轮再试 */
      }
    }
  }, [])

  const startBatch = useCallback(async () => {
    if (batchProcessing || running || !voiceId) return
    setBatchProcessing(true)
    setErrorMessage("")
    try {
      for (const it of queueRef.current) {
        if (it.status === "done") continue
        updateItem(it.id, { status: "running", error: "", url: undefined })
        try {
          await runOfflineVc(it.file, voiceId, pitch, indexRate, denoise, postSeedVc, enhanceLevel, prosody)
          setStatus({
            running: true, status: "running", message: `批量转换：${it.name}`,
            voice_id: voiceId, url: "", duration_s: 0, error: "",
          })
          const s = await waitDone()
          if (s?.status === "done" && s.url) {
            updateItem(it.id, { status: "done", url: s.url, durationS: s.duration_s })
          } else {
            updateItem(it.id, { status: "error", error: s?.error || "转换失败" })
          }
        } catch (error) {
          updateItem(it.id, { status: "error", error: friendlyError(error, "提交失败") })
        }
      }
    } finally {
      setBatchProcessing(false)
    }
  }, [batchProcessing, running, voiceId, pitch, indexRate, denoise, postSeedVc, enhanceLevel, prosody, waitDone, updateItem])

  const canSubmit = !running && !batchProcessing && !submitting && Boolean(audioFile) && Boolean(voiceId)

  const submit = useCallback(async () => {
    if (!canSubmit || !audioFile) return
    setSubmitting(true)
    setErrorMessage("")
    try {
      await runOfflineVc(audioFile, voiceId, pitch, indexRate, denoise, postSeedVc, enhanceLevel, prosody)
      setStatus({
        running: true, status: "running", message: "已提交",
        voice_id: voiceId, url: "", duration_s: 0, error: "",
      })
      startPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "提交失败"))
    } finally {
      setSubmitting(false)
    }
  }, [canSubmit, audioFile, voiceId, pitch, indexRate, denoise, postSeedVc, enhanceLevel, prosody, startPoll])

  useEffect(() => () => {
    stopPoll()
    if (tickTimer.current != null) window.clearInterval(tickTimer.current)
    recorder.current?.stream?.getTracks().forEach((t) => t.stop())
  }, [stopPoll])

  return {
    rvcVoices,
    voiceId,
    setVoiceId,
    recording,
    recordSeconds,
    audioFile,
    pickFile,
    clearFile,
    startRecording,
    stopRecording,
    pitch,
    setPitch,
    pitchAdvice,
    pitchBusy,
    indexRate,
    setIndexRate,
    denoise,
    setDenoise,
    enhanceLevel,
    setEnhanceLevel,
    postSeedVc,
    setPostSeedVc,
    prosody,
    setProsody,
    submitting,
    running,
    canSubmit,
    errorMessage,
    status,
    resultUrl: status?.url ? mediaUrl(status.url) : "",
    submit,
    queue,
    batchProcessing,
    canBatch: !running && !batchProcessing && !submitting && Boolean(voiceId) && queue.some((it) => it.status !== "done"),
    importFiles,
    removeQueueItem,
    clearQueue,
    startBatch,
    presets,
    savePreset,
    applyPreset,
    deletePreset,
    // 深链（首页「录一句听听」带 `?voice=` 过来）
    deepLinkVoiceId: voiceParam ?? "",
    deepLinkGuide,
    deepLinkMiss,
    dismissDeepLinkGuide,
  }
}
