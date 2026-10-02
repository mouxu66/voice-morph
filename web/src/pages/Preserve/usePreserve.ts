import { useCallback, useEffect, useRef, useState } from "react"
import {
  createPreserveProfile,
  deletePreserveProfile,
  getPreserveEvents,
  getPreserveGuide,
  getPreserveProfile,
  listPreserveProfiles,
  recordPreserveConsent,
  runPreserveGate,
  uploadPreserveReference,
  type PreserveEvent,
  type PreserveGate,
  type PreserveGuide,
  type PreserveProfile,
  type PreserveProfileSummary,
} from "@/api/client"
import { friendlyError } from "@/lib/errors"

/**
 * 声音保存页的状态与动作。
 *
 * 设计取舍（都不是随手写的）：
 *
 * 1. **`gate` 不自动跑**。它会真的算客观指标（单个文件约 1 秒），
 *    自动跑会让每次点档案都卡一下。用户点「体检」才跑 ——
 *    与 `docs/音色体检三灯.md` §5 的「GET 只读 / POST 现算」同一套约定。
 * 2. **同意是独立的一步**，不与建档合并。替用户默认同意会让
 *    「仅克隆本人或已授权声音」这条约束形同虚设（后端 `create_profile` 也刻意不写 consent）。
 * 3. **录音目标档先选、再录**。录完才知道该存哪一档，就只能靠猜时长 ——
 *    而档位是研究里的自变量，猜错就把数据污染了。
 */
export function usePreserve() {
  const [guide, setGuide] = useState<PreserveGuide | null>(null)
  const [profiles, setProfiles] = useState<PreserveProfileSummary[]>([])
  const [storeDir, setStoreDir] = useState("")
  const [selectedId, setSelectedId] = useState("")
  const [profile, setProfile] = useState<PreserveProfile | null>(null)
  const [events, setEvents] = useState<PreserveEvent[]>([])
  const [gate, setGate] = useState<PreserveGate | null>(null)

  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState("")
  /** 错误拆成 title/detail —— 与 `ErrorPanel` 的接口一致：
   *  title 说「哪一步出的问题」，detail 放完整原文（用户可以直接复制去问 AI）。 */
  const [error, setError] = useState<{ title: string; detail: string } | null>(null)
  const [notice, setNotice] = useState("")

  /** 正在录的目标档（秒）。0 = 没在录。 */
  const [recordingFor, setRecordingFor] = useState(0)
  const [recordSeconds, setRecordSeconds] = useState(0)

  const fail = useCallback((title: string, err?: unknown) => {
    setError({ title, detail: err == null ? "" : friendlyError(err, "") })
  }, [])

  const recorder = useRef<MediaRecorder | null>(null)
  const chunks = useRef<Blob[]>([])
  const tick = useRef<number | null>(null)

  const clearTimer = useCallback(() => {
    if (tick.current != null) {
      window.clearInterval(tick.current)
      tick.current = null
    }
  }, [])

  const refreshList = useCallback(async () => {
    const [list, summary] = await Promise.all([
      listPreserveProfiles(),
      // 摘要只为了拿「录音存在哪」这一个字段，界面上要如实告诉用户
      import("@/api/client").then((m) => m.getPreserveSummary()),
    ])
    setProfiles(list.profiles)
    setStoreDir(summary.dir)
  }, [])

  const loadProfile = useCallback(async (id: string) => {
    if (!id) {
      setProfile(null)
      setEvents([])
      setGate(null)
      return
    }
    const detail = await getPreserveProfile(id)
    setProfile(detail.profile)
    setEvents(detail.events)
    setGate(null)
  }, [])

  useEffect(() => {
    void (async () => {
      setLoading(true)
      try {
        setGuide(await getPreserveGuide())
        await refreshList()
      } catch (error) {
        fail("读取声音保存数据", error)
      } finally {
        setLoading(false)
      }
    })()
  }, [refreshList])

  useEffect(() => {
    if (!selectedId) return
    void (async () => {
      try {
        await loadProfile(selectedId)
      } catch (error) {
        fail("读取档案", error)
      }
    })()
  }, [selectedId, loadProfile])

  useEffect(() => () => clearTimer(), [clearTimer])

  // ---- 档案 ----

  const create = useCallback(
    async (label: string) => {
      const name = label.trim()
      if (!name) {
        fail("先给这份声音档案起个名字")
        return
      }
      setBusy("create")
      setError(null)
      try {
        const res = await createPreserveProfile(name)
        await refreshList()
        setSelectedId(res.profile.id)
        setNotice("档案已建立。下一步：确认知情同意，然后按引导录音。")
      } catch (error) {
        fail("新建档案", error)
      } finally {
        setBusy("")
      }
    },
    [refreshList]
  )

  const remove = useCallback(
    async (id: string) => {
      setBusy("delete")
      setError(null)
      try {
        await deletePreserveProfile(id)
        if (selectedId === id) setSelectedId("")
        await refreshList()
        setNotice("档案已删除（录音、同意书、日志一并清除）。")
      } catch (error) {
        fail("删除档案", error)
      } finally {
        setBusy("")
      }
    },
    [refreshList, selectedId]
  )

  // ---- 知情同意 ----

  const consent = useCallback(
    async (operator: string) => {
      if (!selectedId) return
      setBusy("consent")
      setError(null)
      try {
        await recordPreserveConsent(selectedId, {
          scope: "self",
          statement: guide?.consent.statement ?? "",
          operator,
        })
        await loadProfile(selectedId)
        setEvents((await getPreserveEvents(selectedId)).events)
        setNotice("已记录知情同意。现在可以录音与体检了。")
      } catch (error) {
        fail("记录知情同意", error)
      } finally {
        setBusy("")
      }
    },
    [guide, loadProfile, selectedId]
  )

  // ---- 录音 ----

  const startRecording = useCallback(
    async (durationS: number) => {
      setError(null)
      setNotice("")
      if (!selectedId) {
        fail("先建一份档案再录音")
        return
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
          ? "audio/webm;codecs=opus"
          : "audio/webm"
        const rec = new MediaRecorder(stream, { mimeType: mime })
        chunks.current = []
        rec.ondataavailable = (e) => {
          if (e.data.size) chunks.current.push(e.data)
        }
        rec.onstop = () => {
          stream.getTracks().forEach((t) => t.stop())
          void (async () => {
            const blob = new Blob(chunks.current, { type: mime })
            const file = new File([blob], "reference.webm", { type: mime })
            setBusy("upload")
            try {
              // 后端会用 ffmpeg 归一成单声道 wav —— 前端不改后缀（改了就是「后缀对、内容错」）
              await uploadPreserveReference(selectedId, file, durationS)
              await loadProfile(selectedId)
              setEvents((await getPreserveEvents(selectedId)).events)
              setNotice("这一档已保存。可以接着录更长的一档，或点「体检」。")
            } catch (error) {
              fail("保存参考音", error)
            } finally {
              setBusy("")
            }
          })()
        }
        rec.start()
        recorder.current = rec
        setRecordingFor(durationS)
        setRecordSeconds(0)
        tick.current = window.setInterval(() => setRecordSeconds((s) => s + 1), 1000)
      } catch (error) {
        fail("打开麦克风", error)
      }
    },
    [loadProfile, selectedId]
  )

  const stopRecording = useCallback(() => {
    recorder.current?.stop()
    recorder.current = null
    setRecordingFor(0)
    clearTimer()
  }, [clearTimer])

  // ---- 门禁 ----

  const runGate = useCallback(async () => {
    if (!selectedId) return
    setBusy("gate")
    setError(null)
    try {
      setGate(await runPreserveGate(selectedId))
    } catch (error) {
      fail("档案体检", error)
    } finally {
      setBusy("")
    }
  }, [selectedId])

  return {
    guide,
    profiles,
    storeDir,
    selectedId,
    setSelectedId,
    profile,
    events,
    gate,
    loading,
    busy,
    error,
    notice,
    setError,
    setNotice,
    recordingFor,
    recordSeconds,
    create,
    remove,
    consent,
    startRecording,
    stopRecording,
    runGate,
  }
}
