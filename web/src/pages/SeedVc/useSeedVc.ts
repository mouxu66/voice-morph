import { useCallback, useEffect, useRef, useState } from "react"
import {
  getSeedVcStatus,
  listVoices,
  mediaUrl,
  runSeedVc,
  type SeedVcStatus,
} from "@/api/client"
import type { VoiceInfo } from "@/types"
import { friendlyError } from "@/lib/errors"

const POLL_MS = 2000

/** Seed-VC 表达力变声：源音频 + 目标参考（voicebank 音色 / 上传参考音）→ 零样本换声，保留或转换语气情绪。 */
export function useSeedVc() {
  const [voices, setVoices] = useState<VoiceInfo[]>([])
  const [voiceId, setVoiceId] = useState("")
  const [useUpload, setUseUpload] = useState(false)
  const [targetFile, setTargetFile] = useState<File | null>(null)
  const [audioFile, setAudioFile] = useState<File | null>(null)
  const [recording, setRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [convertStyle, setConvertStyle] = useState(true)
  // 依 experiments/seedvc_param_sweep.py 实测：sim=0.5 音色相似度最高（0.807）且不漏源
  const [sim, setSim] = useState(0.5)
  const [topP, setTopP] = useState(0.9)
  const [temperature, setTemperature] = useState(1.0)
  const [steps, setSteps] = useState(10)
  const [lenAdjust, setLenAdjust] = useState(1.0)
  const [denoise, setDenoise] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [status, setStatus] = useState<SeedVcStatus | null>(null)
  const pollTimer = useRef<number | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const chunks = useRef<Blob[]>([])
  const tickTimer = useRef<number | null>(null)

  // 音色库清单（有参考音频的 voicebank 音色，零样本无需训练）+ 恢复进行中任务
  useEffect(() => {
    void (async () => {
      try {
        const vs = await listVoices()
        const withRef = vs.filter((v) => v.has_reference !== false)
        setVoices(withRef)
        setVoiceId((cur) => (withRef.some((v) => v.id === cur) ? cur : (withRef[0]?.id ?? "")))
      } catch {
        /* 服务未起，页面有离线提示 */
      }
      try {
        const s = await getSeedVcStatus()
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
      const s = await getSeedVcStatus()
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

  const clearFile = useCallback(() => setAudioFile(null), [])

  const pickTargetFile = useCallback((file: File | undefined) => {
    if (!file) return
    setTargetFile(file)
    setErrorMessage("")
  }, [])

  const clearTargetFile = useCallback(() => setTargetFile(null), [])

  const running = Boolean(status?.running)
  const targetReady = useUpload ? Boolean(targetFile) : Boolean(voiceId)
  const canSubmit = !running && !submitting && Boolean(audioFile) && targetReady

  const submit = useCallback(async () => {
    if (!canSubmit || !audioFile) return
    setSubmitting(true)
    setErrorMessage("")
    try {
      await runSeedVc({
        file: audioFile,
        targetVoiceId: useUpload ? undefined : voiceId,
        targetFile: useUpload ? (targetFile ?? undefined) : undefined,
        convertStyle,
        similarityCfgRate: sim,
        topP,
        temperature,
        diffusionSteps: steps,
        lengthAdjust: lenAdjust,
        denoise,
      })
      setStatus({
        running: true, status: "running", message: "已提交",
        target: useUpload ? "uploaded" : voiceId, url: "", duration_s: 0, error: "",
      })
      startPoll()
    } catch (error) {
      setErrorMessage(friendlyError(error, "提交失败"))
    } finally {
      setSubmitting(false)
    }
  }, [canSubmit, audioFile, useUpload, voiceId, targetFile, convertStyle, sim, topP,
    temperature, steps, lenAdjust, denoise, startPoll])

  useEffect(() => () => {
    stopPoll()
    if (tickTimer.current != null) window.clearInterval(tickTimer.current)
    recorder.current?.stream?.getTracks().forEach((t) => t.stop())
  }, [stopPoll])

  return {
    voices,
    voiceId,
    setVoiceId,
    useUpload,
    setUseUpload,
    targetFile,
    pickTargetFile,
    clearTargetFile,
    audioFile,
    pickFile,
    clearFile,
    recording,
    recordSeconds,
    startRecording,
    stopRecording,
    convertStyle,
    setConvertStyle,
    sim,
    setSim,
    topP,
    setTopP,
    temperature,
    setTemperature,
    steps,
    setSteps,
    lenAdjust,
    setLenAdjust,
    denoise,
    setDenoise,
    submitting,
    running,
    canSubmit,
    errorMessage,
    status,
    resultUrl: status?.url ? mediaUrl(status.url) : "",
    submit,
  }
}
