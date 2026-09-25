import { useCallback, useEffect, useRef, useState } from "react"
import {
  getCoverStatus,
  listRvcVoices,
  mediaUrl,
  runCover,
  suggestCoverPitch,
  type CoverStatus,
  type RvcVoice,
} from "@/api/client"
import { friendlyError } from "@/lib/errors"

const POLL_MS = 2000

/**
 * 「翻唱」页的状态机。
 *
 * 与离线变声（`useOfflineVc`）的三个刻意差异：
 *   1. **中途可能有两次上传**：正式跑一次，若用户点「自动建议变调」则再上传一次
 *      （只算建议、不落地）。所以文件在 state 里留着，不用后即弃。
 *   2. **变调是主角**。离线变声的 pitch 是"微调"，翻唱的 pitch 是"男女声互转"
 *      这种 ±12 的大跳，所以界面上必须显眼（滑块 + 自动建议按钮 + 当前值回显）。
 *   3. **进度带工序名**。翻唱要跑 demucs + RVC，几分钟起步；只给百分比用户会以为
 *      卡死了，所以 step 要翻译成"正在分离人声与伴奏"这种能看懂的话。
 */
export function useCover() {
  const [voices, setVoices] = useState<RvcVoice[]>([])
  const [voiceId, setVoiceId] = useState("")
  const [file, setFile] = useState<File | null>(null)
  const [pitch, setPitch] = useState(0)
  const [autoPitch, setAutoPitch] = useState(true)
  const [indexRate, setIndexRate] = useState(0.5)
  const [vocalGain, setVocalGain] = useState(1)
  const [accompGain, setAccompGain] = useState(1)
  const [submitting, setSubmitting] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [feedback, setFeedback] = useState("")
  const [status, setStatus] = useState<CoverStatus | null>(null)
  const pollTimer = useRef<number | null>(null)

  const stopPoll = useCallback(() => {
    if (pollTimer.current != null) {
      window.clearInterval(pollTimer.current)
      pollTimer.current = null
    }
  }, [])

  const startPoll = useCallback(() => {
    stopPoll()
    pollTimer.current = window.setInterval(async () => {
      try {
        const s = await getCoverStatus()
        setStatus(s)
        if (!s.running) stopPoll()
      } catch {
        /* 下轮再试 */
      }
    }, POLL_MS)
  }, [stopPoll])

  useEffect(() => {
    void (async () => {
      try {
        const r = await listRvcVoices()
        const ready = r.voices.filter((v) => v.model_ready)
        setVoices(ready)
        setVoiceId((cur) => (ready.some((v) => v.id === cur) ? cur : (ready[0]?.id ?? "")))
      } catch {
        /* 后端未起：页面自己有离线提示 */
      }
      try {
        const s = await getCoverStatus()
        setStatus(s)
        if (s.running) startPoll()
      } catch {
        /* ignore */
      }
    })()
    return stopPoll
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (!feedback) return
    const t = window.setTimeout(() => setFeedback(""), 5000)
    return () => window.clearTimeout(t)
  }, [feedback])

  /** 上传歌曲文件（只暂存，不跑） */
  const pickFile = useCallback((f: File | null) => {
    setErrorMessage("")
    setFile(f)
  }, [])

  /** 自动建议变调：上传一次、只算不跑。约 20~40 秒（要分离人声）。 */
  const analyzePitch = useCallback(async () => {
    if (!file) {
      setErrorMessage("请先选择歌曲文件")
      return
    }
    if (!voiceId) {
      setErrorMessage("请先选择音色")
      return
    }
    setErrorMessage("")
    setAnalyzing(true)
    try {
      const r = await suggestCoverPitch(file, voiceId)
      setPitch(r.pitch)
      setAutoPitch(false) // 建议值已经填进滑块，交给用户微调
      setFeedback(
        r.pitch === 0
          ? "音域与你选的角色接近，不需要变调"
          : `已算出建议变调：${r.pitch > 0 ? "+" : ""}${r.pitch} 半音（${r.pitch > 0 ? "升" : "降"}）`
      )
    } catch (e) {
      setErrorMessage(friendlyError(e, "变调分析失败"))
    } finally {
      setAnalyzing(false)
    }
  }, [file, voiceId])

  const start = useCallback(async () => {
    if (!file) {
      setErrorMessage("请先选择歌曲文件")
      return
    }
    if (!voiceId) {
      setErrorMessage("请先选择音色")
      return
    }
    setErrorMessage("")
    setFeedback("")
    setSubmitting(true)
    try {
      await runCover(file, voiceId, pitch, indexRate, vocalGain, accompGain, autoPitch)
      setStatus({
        running: true,
        status: "running",
        step: "separate",
        message: "正在分离人声与伴奏…",
        percent: 5,
        voice_id: voiceId,
        url: "",
        duration_s: 0,
        pitch,
        error: "",
      })
      startPoll()
    } catch (e) {
      setErrorMessage(friendlyError(e, "无法开始翻唱"))
    } finally {
      setSubmitting(false)
    }
  }, [file, voiceId, pitch, indexRate, vocalGain, accompGain, autoPitch, startPoll])

  const running = Boolean(status?.running) || submitting
  const resultUrl = status?.url ? mediaUrl(status.url) : ""

  return {
    voices, voiceId, setVoiceId,
    file, pickFile,
    pitch, setPitch, autoPitch, setAutoPitch,
    indexRate, setIndexRate, vocalGain, setVocalGain, accompGain, setAccompGain,
    submitting, analyzing, errorMessage, feedback, status, running, resultUrl,
    analyzePitch, start,
  }
}
