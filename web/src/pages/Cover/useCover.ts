import { useCallback, useEffect, useRef, useState } from "react"
import {
  fetchCoverUrl,
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
 *   4. **两种来源二选一**：本地文件（上传）或粘一条直链（后端下到会话目录）。
 *      两者都住会话目录、都"跑完即删"，所以往下只需要记一个 `srcName`。
 */
export function useCover() {
  const [voices, setVoices] = useState<RvcVoice[]>([])
  const [voiceId, setVoiceId] = useState("")
  const [file, setFile] = useState<File | null>(null)
  /** 会话里已下好的源文件名（粘直链那条路）；与 file 二选一 */
  const [srcName, setSrcName] = useState("")
  /** 粘的链接（留着 —— 开跑后源被删掉，想再跑一次只需重新点「下载并试听」） */
  const [srcUrl, setSrcUrl] = useState("")
  /** 已下好源的试听地址（后端返回的相对路径）与一句话说明 */
  const [srcPreview, setSrcPreview] = useState("")
  const [srcSummary, setSrcSummary] = useState("")
  const [fetching, setFetching] = useState(false)
  const [pitch, setPitch] = useState(0)
  const [autoPitch, setAutoPitch] = useState(true)
  const [indexRate, setIndexRate] = useState(0.5)
  const [vocalGain, setVocalGain] = useState(1)
  const [accompGain, setAccompGain] = useState(1)
  /**
   * 自动配平（默认开，与后端默认一致）：按实测 RMS 配人声/伴奏音量比。
   * 勾上时上面两个增益滑块**不生效**（后端以实测值为准）—— 所以界面必须在
   * 勾选态把它们置灰并说清"现在以实测为准"，完成后再把实测值显示出来。
   * 这三个口径是 2026-09-27 一起补的：此前前端不传 auto_gain、也没有开关，
   * 用户拖滑块毫无效果且看不到实测增益，等于两块死控件加一次暗箱。
   */
  const [autoGain, setAutoGain] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [analyzing, setAnalyzing] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [feedback, setFeedback] = useState("")
  const [status, setStatus] = useState<CoverStatus | null>(null)
  /**
   * 结果区那张「翻唱完成」面板是不是**上一次**的成果。
   *
   * 唯一的置真路径：`start()` 提交失败（后端 409/500/断网）—— 此时 status 还是
   * 上一轮的 done，于是「翻唱失败」错误条与旧成品同屏，用户完全分不清错误说的
   * 是面板里那首歌还是刚点的这次（2026-09-27 修）。新一轮成功提交时置假。
   * 轮询跑到新 done 时面板自然被替换，不需要额外复位。
   */
  const [resultStale, setResultStale] = useState(false)
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

  /** 上传歌曲文件（只暂存，不跑）。选了文件就放弃直链那份 —— 来源永远只有一个 */
  const pickFile = useCallback((f: File | null) => {
    setErrorMessage("")
    setFile(f)
    if (f) {
      setSrcName("")
      setSrcPreview("")
      setSrcSummary("")
    }
  }, [])

  /** 粘直链 → 下到会话目录 → 可试听（**不跑链路**）。下错歌只花几秒，不是三分钟。 */
  const fetchFromUrl = useCallback(async () => {
    const url = srcUrl.trim()
    if (!url) {
      setErrorMessage("请先粘贴一条音频直链")
      return
    }
    setErrorMessage("")
    setFeedback("")
    setFetching(true)
    try {
      const r = await fetchCoverUrl(url)
      setSrcName(r.name)
      setSrcPreview(mediaUrl(r.url))
      setSrcSummary(
        `${(r.bytes / 1024 / 1024).toFixed(1)} MB` +
          (r.duration_s
            ? ` · ${Math.floor(r.duration_s / 60)}:${String(Math.round(r.duration_s % 60)).padStart(2, "0")}`
            : "")
      )
      setFile(null)
      // 主动把来源切成直链时要说出来 —— 静默清掉本地文件会让用户以为
      // "我刚选的那首怎么没了"（2026-09-27 修，与 clearSource 的可见性对齐）。
      setFeedback(
        file
          ? "已下好，来源已切换为这条直链（原来选的本地文件已放弃）—— 试听确认后再点「开始翻唱」"
          : "已下好，可以先试听 —— 确认是这首歌再点「开始翻唱」"
      )
    } catch (e) {
      setSrcName("")
      setSrcPreview("")
      setSrcSummary("")
      setErrorMessage(friendlyError(e, "下载失败"))
    } finally {
      setFetching(false)
    }
  }, [srcUrl, file])

  /** 丢掉直链那份（改用文件，或换一首） */
  const clearSource = useCallback(() => {
    setSrcName("")
    setSrcPreview("")
    setSrcSummary("")
  }, [])

  /** 自动建议变调：上传一次、只算不跑。约 20~40 秒（要分离人声）。 */
  const analyzePitch = useCallback(async () => {
    if (!file && !srcName) {
      setErrorMessage("请先选择歌曲文件，或粘贴一条音频直链")
      return
    }
    if (!voiceId) {
      setErrorMessage("请先选择音色")
      return
    }
    setErrorMessage("")
    setAnalyzing(true)
    try {
      const r = await suggestCoverPitch(file, voiceId, srcName)
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
  }, [file, voiceId, srcName])

  const start = useCallback(async () => {
    if (!file && !srcName) {
      setErrorMessage("请先选择歌曲文件，或粘贴一条音频直链")
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
      await runCover(file, voiceId, pitch, indexRate, vocalGain, accompGain, autoPitch, srcName, autoGain)
      // 源已经被这条任务用掉了（后端跑完就删，即用即删）。这里同步清掉试听，
      // 免得用户对着一个已经不在的文件再点一次「开始翻唱」而拿到 400。
      clearSource()
      setResultStale(false) // 新一轮已提交：后续 done 面板就是这次的成果
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
        vocal_gain_applied: 0,
        error: "",
      })
      startPoll()
    } catch (e) {
      // 旧成品还挂在结果区。不标记的话，「翻唱失败」与「翻唱完成」同屏，
      // 用户分不清错误说的是哪一次（2026-09-27 修）。
      setResultStale(true)
      setErrorMessage(friendlyError(e, "无法开始翻唱"))
    } finally {
      setSubmitting(false)
    }
  }, [file, voiceId, pitch, indexRate, vocalGain, accompGain, autoPitch, srcName, autoGain, clearSource, startPoll])

  const running = Boolean(status?.running) || submitting
  const resultUrl = status?.url ? mediaUrl(status.url) : ""

  return {
    voices, voiceId, setVoiceId,
    file, pickFile,
    srcName, srcUrl, setSrcUrl, srcPreview, srcSummary, fetching, fetchFromUrl, clearSource,
    pitch, setPitch, autoPitch, setAutoPitch,
    indexRate, setIndexRate, vocalGain, setVocalGain, accompGain, setAccompGain, autoGain, setAutoGain,
    submitting, analyzing, errorMessage, feedback, status, running, resultUrl, resultStale,
    analyzePitch, start,
    /** 有来源（本地文件或已下好的直链）才允许开跑/分析音域 */
    hasSource: Boolean(file || srcName),
  }
}
