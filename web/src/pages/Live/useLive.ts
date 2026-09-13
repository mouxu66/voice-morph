import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  exportRvcDataset,
  generateRvcDataset,
  getLiveAudioDevices,
  getRvcGenStatus,
  listRvcDataset,
  listRvcVoices,
  rvcLiveMonitor,
  rvcLiveSetProfile,
  rvcLiveStart,
  rvcLiveStatus,
  rvcLiveStop,
  rvcTrainStart,
  rvcTrainStatus,
  setLiveAudioDevices,
  type LiveAudioDevices,
  type RvcGenStatus,
  type RvcLiveStatus,
  type RvcTrainStatus,
  type RvcVoicesInfo,
} from "@/api/client"

/**
 * 实时变声页的状态机。
 *
 * 核心修复：音色是可选择的。以前实时页永远用后端"当前生效实验"，
 * 用户在音色库选了 A，实时页却跑着 B —— 于是"选了却不像"。
 * 现在每一步（生成语料 / 导入 RVC / 训练 / 变声）都绑定选中的音色 ID。
 */
const LS_KEY = "vm_rvc_exp"

type Feedback = { tone: "ok" | "error" | "info"; text: string }

function msgOf(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : String(error)
  return raw || fallback
}

export type RvcStepKey = "corpus" | "import" | "train" | "live"

export function useLive() {
  const [voicesInfo, setVoicesInfo] = useState<RvcVoicesInfo | null>(null)
  const [selectedExp, setSelectedExp] = useState<string | null>(() => {
    try {
      return localStorage.getItem(LS_KEY)
    } catch {
      return null
    }
  })
  const [liveStatus, setLiveStatus] = useState<RvcLiveStatus | null>(null)
  const [trainStatus, setTrainStatus] = useState<RvcTrainStatus | null>(null)
  const [genStatus, setGenStatus] = useState<RvcGenStatus | null>(null)
  const [generatedCount, setGeneratedCount] = useState(0)

  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [training, setTraining] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  // A7/A8：输入设备清单与降噪开关设置（挂载时加载 + 保存后刷新）
  const [audioDevices, setAudioDevices] = useState<LiveAudioDevices | null>(null)

  // 供按钮点击后立即刷新（不必等下一次轮询）
  const tickRef = useRef<() => void>(() => {})

  // 音色清单里没有当前选中项时（首次加载 / 音色被删 / 换机器），自动挑一个最合适的
  useEffect(() => {
    if (!voicesInfo) return
    const ids = new Set(voicesInfo.voices.map((v) => v.id))
    if (selectedExp && ids.has(selectedExp)) return
    const preferred = [
      voicesInfo.active_exp,
      voicesInfo.voices.find((v) => v.model_ready)?.id,
      voicesInfo.voices[0]?.id,
    ].find((id): id is string => !!id && ids.has(id))
    setSelectedExp(preferred ?? null)
  }, [voicesInfo, selectedExp])

  useEffect(() => {
    try {
      if (selectedExp) localStorage.setItem(LS_KEY, selectedExp)
    } catch {
      /* 隐私模式下写不了，忽略 */
    }
  }, [selectedExp])

  const busy = Boolean(
    trainStatus?.running || genStatus?.running || liveStatus?.live_running,
  )

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [vi, gs] = await Promise.all([listRvcVoices(), getRvcGenStatus()])
        if (!alive) return
        setVoicesInfo(vi)
        setGenStatus(gs)
        if (!selectedExp) {
          setLiveStatus(null)
          setTrainStatus(null)
          setGeneratedCount(0)
          return
        }
        const [ls, ts, ds] = await Promise.all([
          rvcLiveStatus(selectedExp),
          rvcTrainStatus(selectedExp),
          listRvcDataset(selectedExp),
        ])
        if (!alive) return
        setLiveStatus(ls)
        setTrainStatus(ts)
        setGeneratedCount(ds.items.length)
      } catch {
        /* 后端未启动时静默，页面顶部有服务状态指示 */
      }
    }
    tickRef.current = () => void tick()
    void tick()
    const timer = window.setInterval(() => void tick(), busy ? 1500 : 5000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [selectedExp, busy])

  const liveOn = Boolean(liveStatus?.live_running)
  // 无头模式：进程活着但模型还在加载（音频流未就绪）时显示「加载中」
  const liveReady = liveOn ? liveStatus?.live_ready !== false : false
  const monitorOn = Boolean(liveStatus?.monitor_on)
  // 正在跑实时变声的音色（后端 active_exp），用于"选了别的音色要先停"的提示
  const liveExp = liveOn ? voicesInfo?.active_exp ?? null : null
  const current = useMemo(
    () => voicesInfo?.voices.find((v) => v.id === selectedExp) ?? null,
    [voicesInfo, selectedExp],
  )
  const modelOk = Boolean(current?.model_ready)
  const datasetCount = current?.dataset_count ?? 0

  const steps = useMemo(
    () => [
      { key: "corpus" as RvcStepKey, label: "生成语料", hint: "用该音色合成训练句", done: generatedCount > 0 },
      { key: "import" as RvcStepKey, label: "导入 RVC", hint: "同步到训练集目录", done: datasetCount > 0 },
      { key: "train" as RvcStepKey, label: "训练模型", hint: "提取特征并训练权重", done: modelOk },
      { key: "live" as RvcStepKey, label: "实时变声", hint: "切虚拟声卡开始变声", done: liveOn },
    ],
    [generatedCount, datasetCount, modelOk, liveOn],
  )
  const activeStep = steps.findIndex((s) => !s.done)

  const generateCorpus = useCallback(async () => {
    if (!selectedExp) return
    setGenerating(true)
    setFeedback(null)
    try {
      const r = await generateRvcDataset(selectedExp)
      setFeedback({ tone: "ok", text: `已开始生成 ${r.total} 句训练语料，下方显示进度。` })
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "语料生成失败") })
    } finally {
      setGenerating(false)
      tickRef.current()
    }
  }, [selectedExp])

  const importCorpus = useCallback(async () => {
    if (!selectedExp) return
    setImporting(true)
    setFeedback(null)
    try {
      const r = await exportRvcDataset(selectedExp)
      setFeedback({ tone: "ok", text: `已导入 ${r.copied} 条语料到 RVC 训练集目录。` })
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "导入失败") })
    } finally {
      setImporting(false)
      tickRef.current()
    }
  }, [selectedExp])

  const train = useCallback(async () => {
    if (!selectedExp) return
    setTraining(true)
    setFeedback(null)
    try {
      const r = await rvcTrainStart({ expName: selectedExp })
      setFeedback({
        tone: "ok",
        text: r.already_running
          ? "训练已在后台进行中，下方实时显示进度。"
          : "已启动训练，下方会实时显示各阶段进度（另开了控制台窗口可看完整日志）。",
      })
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "训练启动失败") })
    } finally {
      setTraining(false)
      tickRef.current()
    }
  }, [selectedExp])

  const start = useCallback(async () => {
    if (!selectedExp) return
    setStarting(true)
    setFeedback(null)
    try {
      const r = await rvcLiveStart(selectedExp)
      setFeedback({
        tone: "ok",
        text: r.hint ?? (r.already_running ? "实时变声已在运行中。" : "已启动实时变声。"),
      })
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "启动失败") })
    } finally {
      setStarting(false)
      tickRef.current()
    }
  }, [selectedExp])

  const stop = useCallback(async () => {
    setStopping(true)
    setFeedback(null)
    try {
      const r = await rvcLiveStop()
      if (!r.ok) {
        setFeedback({
          tone: "error",
          text: `停止失败：${r.error ?? "未知错误"}。若设备没恢复，请到左侧栏点「一键恢复音频」。`,
        })
        return
      }
      setFeedback({
        tone: "ok",
        text: r.note
          ? `${r.note}。注意：已打开的微信/游戏等应用不会自动切换录音设备，请退出后重新打开即可恢复。`
          : "已停止实时变声并还原声卡。注意：已打开的微信/游戏等应用不会自动跟随设备切换，请退出后重新打开即可恢复。",
      })
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "停止失败") })
    } finally {
      setStopping(false)
      tickRef.current()
    }
  }, [])

  const toggleMonitor = useCallback(
    async (on: boolean, gain?: number) => {
      setFeedback(null)
      try {
        const r = await rvcLiveMonitor(on, gain)
        if (!r.ok) {
          setFeedback({ tone: "error", text: "监听开关失败，请稍后重试。" })
        } else {
          setFeedback(on
            ? { tone: "ok", text: "自我监听已开：耳机里能听到变声后的自己（注意音量，过大可能啸叫）。" }
            : { tone: "info", text: "自我监听已关：变声只送给微信/游戏，自己不再听到。" })
        }
      } catch (error) {
        setFeedback({ tone: "error", text: msgOf(error, "监听开关失败") })
      } finally {
        tickRef.current()
      }
    },
    [],
  )

  // ---- A7/A8：输入设备选择 + 输入降噪 ----

  const refreshAudioDevices = useCallback(async () => {
    try {
      setAudioDevices(await getLiveAudioDevices())
    } catch {
      /* 后端未启动时静默，与主轮询一致 */
    }
  }, [])

  // 挂载时拉一次；插拔 USB 麦/手机连接后可点刷新按钮
  useEffect(() => {
    void refreshAudioDevices()
  }, [refreshAudioDevices])

  const saveAudioDevice = useCallback(
    async (keyword: string) => {
      setFeedback(null)
      try {
        const r = await setLiveAudioDevices({ input_device: keyword })
        setAudioDevices((prev) =>
          prev ? { ...prev, explicit: r.input_device, running: r.running } : prev,
        )
        setFeedback(
          r.needs_restart
            ? { tone: "info", text: "输入麦克风已保存，重启变声后生效。" }
            : { tone: "ok", text: "输入麦克风已保存。" },
        )
      } catch (error) {
        setFeedback({ tone: "error", text: msgOf(error, "保存输入麦克风失败") })
        void refreshAudioDevices() // 校验失败时回读真实状态，避免下拉显示假值
      }
    },
    [refreshAudioDevices],
  )

  const toggleDenoise = useCallback(
    async (on: boolean) => {
      setFeedback(null)
      try {
        const r = await setLiveAudioDevices({ denoise: on })
        setAudioDevices((prev) => (prev ? { ...prev, denoise: r.denoise } : prev))
        setFeedback(
          r.needs_restart
            ? { tone: "info", text: on ? "输入降噪已开启，重启变声后生效。" : "输入降噪已关闭，重启变声后生效。" }
            : { tone: on ? "ok" : "info", text: on ? "输入降噪已开启。" : "输入降噪已关闭。" },
        )
      } catch (error) {
        setFeedback({ tone: "error", text: msgOf(error, "降噪开关失败") })
        void refreshAudioDevices()
      }
    },
    [refreshAudioDevices],
  )

  // ---- 性能档位（balanced/game） + GPU 显存 ----

  const perfProfile = liveStatus?.perf_profile
  const perfProfileDesc = liveStatus?.perf_profile_desc

  const setProfile = useCallback(
    async (profile: string) => {
      if (profile === perfProfile) return
      setRestarting(true)
      setFeedback(null)
      try {
        const r = await rvcLiveSetProfile(profile)
        setFeedback({
          tone: "ok",
          text: r.restarted
            ? `已切换为「${r.profile_desc ?? profile}」，正在自动重启变声。`
            : `已保存为「${r.profile_desc ?? profile}」，下次开启变声生效。`,
        })
      } catch (error) {
        setFeedback({ tone: "error", text: msgOf(error, "切换性能档位失败") })
      } finally {
        setRestarting(false)
        tickRef.current()
      }
    },
    [perfProfile],
  )

  return {
    voicesInfo,
    voices: voicesInfo?.voices ?? [],
    selectedExp,
    selectExp: setSelectedExp,
    current,
    modelOk,
    datasetCount,
    generatedCount,
    liveStatus,
    liveOn,
    liveReady,
    monitorOn,
    liveExp,
    trainStatus,
    genStatus,
    steps,
    activeStep,
    starting,
    stopping,
    restarting,
    training,
    generating,
    importing,
    feedback,
    perfProfile,
    perfProfileDesc,
    gpuTotalMb: liveStatus?.gpu_total_mb ?? null,
    gpuUsedMb: liveStatus?.gpu_used_mb ?? null,
    liveProcVramMb: liveStatus?.live_proc_vram_mb ?? null,
    start,
    stop,
    setProfile,
    toggleMonitor,
    audioDevices,
    refreshAudioDevices,
    saveAudioDevice,
    toggleDenoise,
    train,
    generateCorpus,
    importCorpus,
  }
}
