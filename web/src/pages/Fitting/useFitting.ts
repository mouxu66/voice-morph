import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  fittingBuiltinSource,
  fittingCancel,
  fittingDeleteSource,
  fittingEnv,
  fittingSources,
  fittingTask,
  fittingTry,
  fittingUploadSource,
  listRvcVoices,
  marketInstalled,
  marketManifest,
  mediaUrl,
  rvcLiveMonitor,
  rvcLiveStart,
  rvcLiveStatus,
  rvcLiveStop,
  type FittingEnv,
  type FittingSource,
  type FittingTask,
  type MarketItem,
  type RvcVoice,
} from "@/api/client"
import { friendlyError } from "@/lib/errors"
import { notify } from "@/lib/notify"
import { setOvcHandoff } from "@/lib/ovcHandoff"

export type WardrobeVoice = {
  /** RVC 实验名 / 市场 voice_id —— 语音链路一律用这个 key（voicebank id 是另一套命名） */
  id: string
  name: string
  origin: "mine" | "market"
  /** 本机已有可推理权重（可直接试穿，不用下载） */
  ready: boolean
  /** 有参考音 → 支持「文字试穿」（市场 RVC 权重没有参考音） */
  hasReference: boolean
  category?: string
  image?: string
  sizeHintMb?: number
  license?: string
}

export type FittingFilter = "all" | "ready" | "mine" | "market"

// 环境探测要起 PowerShell 查进程（单次约 2~3 秒），所以轮询要克制：
// 只在空闲时轮，跑任务时没必要（按钮本来就被任务状态禁掉了）。
const ENV_POLL_MS = 8000
const TASK_POLL_MS = 2000

export function useFitting() {
  const [env, setEnv] = useState<FittingEnv | null>(null)
  const [myVoices, setMyVoices] = useState<RvcVoice[]>([])
  const [market, setMarket] = useState<MarketItem[]>([])
  const [installed, setInstalled] = useState<string[]>([])

  const [selected, setSelected] = useState<string[]>([])
  const [mode, setMode] = useState<"audio" | "text">("audio")
  const [text, setText] = useState("大家好，这是我的新声音，你觉得怎么样？")
  const [pitch, setPitch] = useState(0)
  const [indexRate, setIndexRate] = useState(0.5)
  /** 是否算客观分：关掉能省一次打分器加载（显存紧时更稳），结果照常出 */
  const [score, setScore] = useState(true)

  const [source, setSource] = useState<FittingSource | null>(null)
  const [sources, setSources] = useState<FittingSource[]>([])
  const [task, setTask] = useState<FittingTask | null>(null)

  const [filter, setFilter] = useState<FittingFilter>("all")
  const [keyword, setKeyword] = useState("")
  const [uploading, setUploading] = useState(false)
  const [recording, setRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const [wardrobeError, setWardrobeError] = useState("")

  const [liveExp, setLiveExp] = useState("")
  const [liveRunning, setLiveRunning] = useState(false)
  const [liveBusy, setLiveBusy] = useState("")
  const [liveMonitorOn, setLiveMonitorOn] = useState(false)

  const recorder = useRef<MediaRecorder | null>(null)
  const chunks = useRef<Blob[]>([])
  const tick = useRef<number | null>(null)

  // ---- 环境态势 ----
  const refreshEnv = useCallback(async () => {
    try {
      setEnv(await fittingEnv())
    } catch {
      /* 环境探测失败不打断使用，按钮会按最保守的假设禁用 */
    }
  }, [])

  // ---- 衣柜 ----
  const refreshWardrobe = useCallback(async () => {
    setWardrobeError("")
    try {
      const [voices, items, inst] = await Promise.all([
        listRvcVoices(),
        marketManifest(),
        marketInstalled().catch(() => [] as string[]),
      ])
      setMyVoices(voices.voices)
      setMarket(items)
      setInstalled(inst)
    } catch (e) {
      setWardrobeError(friendlyError(e, "音色列表读取失败"))
    }
  }, [])

  // ---- 源音频 ----
  const refreshSources = useCallback(async () => {
    try {
      const list = await fittingSources()
      setSources(list)
      return list
    } catch {
      return [] as FittingSource[]
    }
  }, [])

  // ---- 任务 ----
  const refreshTask = useCallback(async () => {
    try {
      setTask(await fittingTask())
    } catch {
      /* 轮询失败下次再来 */
    }
  }, [])

  const refreshLive = useCallback(async () => {
    try {
      const st = await rvcLiveStatus()
      setLiveRunning(!!st.live_running)
      setLiveExp(st.exp || "")
      setLiveMonitorOn(!!st.monitor_on)
    } catch {
      /* 同上 */
    }
  }, [])

  // ---- 首屏：环境 + 衣柜 + 源 + 任务 + 实时状态 ----
  useEffect(() => {
    void refreshEnv()
    void refreshWardrobe()
    void refreshLive()
    void refreshTask()
    void (async () => {
      const list = await refreshSources()
      if (list.length) {
        setSource((cur) => cur ?? list[0])
        return
      }
      // 没有源音频时先备一份内置示范片段 —— 不录音也能立刻试穿
      try {
        const builtin = await fittingBuiltinSource()
        setSource(builtin)
        void refreshSources()
      } catch {
        /* 内置源句缺失（极少见）：等用户自己录 */
      }
    })()
  }, [refreshEnv, refreshWardrobe, refreshLive, refreshSources, refreshTask])

  // ---- 环境轮询：只在空闲时轮（跑任务时按钮已被任务状态禁掉，再探测没意义） ----
  useEffect(() => {
    if (task?.running || task?.scoring) return
    const id = window.setInterval(() => void refreshEnv(), ENV_POLL_MS)
    return () => window.clearInterval(id)
  }, [refreshEnv, task?.running, task?.scoring])

  // ---- 任务轮询：推理中每 2 秒看一次；打分阶段继续轮（分数是陆续填进来的） ----
  useEffect(() => {
    if (!task?.running && !task?.scoring) return
    const id = window.setInterval(() => void refreshTask(), TASK_POLL_MS)
    return () => window.clearInterval(id)
  }, [refreshTask, task?.running, task?.scoring])

  // 任务刚结束时补一次环境与实时状态（GPU 释放了，按钮该重新可用）
  const wasRunning = useRef(false)
  useEffect(() => {
    const running = !!task?.running
    if (wasRunning.current && !running) {
      void refreshEnv()
      void refreshLive()
      if (task?.status === "done") {
        const ok = (task.results || []).filter((r) => r.status === "done").length
        const bad = (task.results || []).filter((r) => r.status === "failed").length
        if (ok && !bad) notify.success(`试穿完成：${ok} 件都出来了`)
        else if (ok && bad) notify.warn(`试穿完成：成功 ${ok} 件、失败 ${bad} 件`, "失败原因见对应卡片")
        else if (bad) notify.error(`试穿失败：${bad} 件都没跑出来`, "原因见对应卡片")
      }
    }
    wasRunning.current = running
  }, [task?.running, task?.status, task?.results, refreshEnv, refreshLive])

  // ---- 衣柜合并：市场清单 + 本机音色（同 key 时以本机为准，保留市场的中文名与配图） ----
  const wardrobe = useMemo<WardrobeVoice[]>(() => {
    const map = new Map<string, WardrobeVoice>()
    for (const it of market) {
      const id = (it.voice_id || it.id || "").trim()
      if (!id) continue
      map.set(id, {
        id,
        name: it.name || id,
        origin: "market",
        ready: false,
        hasReference: false,
        category: it.category,
        image: it.image,
        sizeHintMb: it.size_hint_mb,
        license: it.license,
      })
    }
    for (const v of myVoices) {
      const prev = map.get(v.id)
      map.set(v.id, {
        id: v.id,
        name: v.display_name || prev?.name || v.id,
        origin: prev ? "market" : "mine",
        ready: !!v.model_ready,
        hasReference: !!v.has_reference,
        category: prev?.category,
        image: prev?.image,
        sizeHintMb: prev?.sizeHintMb,
        license: prev?.license,
      })
    }
    return [...map.values()].sort((a, b) => {
      if (a.ready !== b.ready) return a.ready ? -1 : 1
      return a.name.localeCompare(b.name, "zh")
    })
  }, [market, myVoices])

  const visibleWardrobe = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    return wardrobe.filter((v) => {
      if (filter === "ready" && !v.ready) return false
      if (filter === "mine" && v.origin !== "mine") return false
      if (filter === "market" && v.origin !== "market") return false
      if (!kw) return true
      return v.id.toLowerCase().includes(kw) || v.name.toLowerCase().includes(kw)
    })
  }, [wardrobe, filter, keyword])

  const nameOf = useCallback(
    (id: string) => wardrobe.find((v) => v.id === id)?.name ?? id,
    [wardrobe],
  )

  // ---- 选择 ----
  const toggleVoice = useCallback((id: string) => {
    setSelected((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))
  }, [])
  const clearSelection = useCallback(() => setSelected([]), [])
  const selectAllVisible = useCallback(() => {
    setSelected((cur) => {
      const next = new Set(cur)
      for (const v of visibleWardrobe) next.add(v.id)
      return [...next]
    })
  }, [visibleWardrobe])

  // ---- 源音频 ----
  const applySource = useCallback(
    async (fn: () => Promise<FittingSource>) => {
      setUploading(true)
      try {
        const s = await fn()
        setSource(s)
        void refreshSources()
        return s
      } catch (e) {
        notify.error(friendlyError(e, "音频处理失败"))
        return null
      } finally {
        setUploading(false)
      }
    },
    [refreshSources],
  )

  const useBuiltinSource = useCallback(
    () => applySource(fittingBuiltinSource),
    [applySource],
  )
  const useFileSource = useCallback(
    (file: File | undefined) => {
      if (!file) return
      void applySource(() => fittingUploadSource(file, file.name))
    },
    [applySource],
  )
  const pickSource = useCallback(
    (id: string) => {
      const s = sources.find((x) => x.source_id === id)
      if (s) setSource(s)
    },
    [sources],
  )
  const removeSource = useCallback(
    async (id: string) => {
      try {
        await fittingDeleteSource(id)
        setSource((cur) => (cur?.source_id === id ? null : cur))
        const list = await refreshSources()
        if (!source || source.source_id === id) setSource(list[0] ?? null)
      } catch (e) {
        notify.error(friendlyError(e, "删除源音频失败"))
      }
    },
    [refreshSources, source],
  )

  // ---- 录音 ----
  const startRecording = useCallback(async () => {
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
        const blob = new Blob(chunks.current, { type: mime })
        void applySource(() => fittingUploadSource(blob, "fitting_recording.webm"))
      }
      rec.start()
      recorder.current = rec
      setRecording(true)
      setRecordSeconds(0)
      tick.current = window.setInterval(() => setRecordSeconds((s) => s + 1), 1000)
    } catch (e) {
      notify.error(friendlyError(e, "无法访问麦克风，请检查系统权限"))
    }
  }, [applySource])

  const stopRecording = useCallback(() => {
    recorder.current?.stop()
    recorder.current = null
    setRecording(false)
    if (tick.current != null) {
      window.clearInterval(tick.current)
      tick.current = null
    }
  }, [])

  useEffect(() => {
    return () => {
      if (tick.current != null) window.clearInterval(tick.current)
    }
  }, [])

  // ---- 批量试穿 ----
  const batchRunning = !!task?.running
  const canRun = useMemo(() => {
    if (!selected.length || batchRunning || uploading || recording) return false
    if (!env?.batch_ready) return false
    if (mode === "audio") return !!source
    return text.trim().length > 0
  }, [selected.length, batchRunning, uploading, recording, env?.batch_ready, mode, source, text])

  const runBlockReason = useMemo(() => {
    if (batchRunning) return "正在试穿，等这一轮跑完"
    if (!selected.length) return "先在左边衣柜里挑几件（可多选）"
    if (env && !env.batch_ready) return env.busy_reason || "显卡显存不足，先关掉占显存的程序"
    if (mode === "audio" && !source) return "先录一段或上传一段声音当「身体」"
    if (mode === "text" && !text.trim()) return "先写下要合成的话"
    return ""
  }, [batchRunning, selected.length, env, mode, source, text])

  const run = useCallback(async () => {
    try {
      const res = await fittingTry({
        voice_ids: selected,
        source_id: mode === "audio" ? source?.source_id ?? "" : "",
        text: mode === "text" ? text.trim() : "",
        pitch,
        index_rate: indexRate,
        score,
      })
      notify.info(`开始试穿 ${res.total} 件，每件约 10 秒到 3 分钟，结果会陆续出来`)
      void refreshTask()
    } catch (e) {
      notify.error(friendlyError(e, "试穿提交失败"))
    }
  }, [selected, mode, source, text, pitch, indexRate, score, refreshTask])

  const cancel = useCallback(async () => {
    try {
      const r = await fittingCancel()
      notify.info(r.cancelled ? "已请求取消，当前这件跑完就停" : "当前没有试穿任务")
    } catch (e) {
      notify.error(friendlyError(e, "取消失败"))
    }
  }, [])

  // ---- 实时试穿：手动轮换（点哪件换哪件，不做自动连跑） ----
  const startLive = useCallback(
    async (voiceId: string) => {
      const name = nameOf(voiceId)
      if (batchRunning) {
        notify.warn("批量试穿正在跑，先等它结束再开实时（两个都抢显卡）")
        return
      }
      setLiveBusy(voiceId)
      try {
        const st = await rvcLiveStatus()
        if (st.live_running && st.exp && st.exp !== voiceId) {
          notify.info(`正在从「${st.exp}」换到「${name}」，需要重载模型，约 10 来秒…`)
          await rvcLiveStop()
        }
        const res = await rvcLiveStart(voiceId, { monitor: true })
        if (res.already_running) {
          notify.info(`「${name}」已经在跑了，直接说话就能听到`)
        } else {
          notify.success(`已换上「${name}」`, "戴上耳机对着麦克风说话，就能听到变声后的自己")
        }
        // game 档默认关自我监听，这里再确保打开一次（自己说话要听得见）
        try {
          await rvcLiveMonitor(true)
        } catch {
          /* 监听开不上不影响变声本身 */
        }
        void refreshLive()
        void refreshEnv()
      } catch (e) {
        notify.error(friendlyError(e, "实时试穿启动失败"), "若提示显存不足，先关掉占显卡的程序")
      } finally {
        setLiveBusy("")
      }
    },
    [batchRunning, nameOf, refreshEnv, refreshLive],
  )

  const stopLive = useCallback(async () => {
    setLiveBusy("__stop__")
    try {
      await rvcLiveStop()
      notify.info("已停止实时试穿，声卡已还原")
      void refreshLive()
      void refreshEnv()
    } catch (e) {
      notify.error(friendlyError(e, "停止失败"))
    } finally {
      setLiveBusy("")
    }
  }, [refreshEnv, refreshLive])

  const toggleMonitor = useCallback(async () => {
    try {
      const r = await rvcLiveMonitor(!liveMonitorOn)
      setLiveMonitorOn(!!r.monitor_on)
      notify.info(r.monitor_on ? "已打开自我监听" : "已关闭自我监听")
    } catch (e) {
      notify.error(friendlyError(e, "自我监听切换失败"))
    }
  }, [liveMonitorOn])

  // ---- 拿去微调：把试穿产物交给离线变声页继续调参（复用既有交接机制） ----
  const handoffToOfflineVc = useCallback(
    async (url: string, voiceId: string) => {
      try {
        // 必过 mediaUrl：桌面端页面是 file://，裸拼相对路径会打到 file:// 而不是后端
        const res = await fetch(mediaUrl(url))
        const blob = await res.blob()
        const file = new File([blob], `fitting-${voiceId}.wav`, { type: "audio/wav" })
        setOvcHandoff([file])
        notify.success("已把这段音频带到离线变声页", "切过去就能继续调音高与检索强度")
        return true
      } catch (e) {
        notify.error(friendlyError(e, "交接失败"))
        return false
      }
    },
    [],
  )

  return {
    env,
    wardrobe,
    visibleWardrobe,
    marketCount: market.length,
    myCount: myVoices.length,
    installedCount: installed.length,
    wardrobeError,
    filter,
    setFilter,
    keyword,
    setKeyword,
    selected,
    toggleVoice,
    clearSelection,
    selectAllVisible,
    nameOf,

    mode,
    setMode,
    text,
    setText,
    pitch,
    setPitch,
    indexRate,
    setIndexRate,
    score,
    setScore,

    source,
    sources,
    pickSource,
    removeSource,
    useBuiltinSource,
    useFileSource,
    uploading,
    recording,
    recordSeconds,
    startRecording,
    stopRecording,

    task,
    results: task?.results ?? [],
    batchRunning,
    scoring: !!task?.scoring,
    run,
    cancel,
    canRun,
    runBlockReason,

    liveExp,
    liveRunning,
    liveBusy,
    liveMonitorOn,
    startLive,
    stopLive,
    toggleMonitor,

    handoffToOfflineVc,
    refreshEnv,
    refreshWardrobe,
    refreshTask,
  }
}
