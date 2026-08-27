import { useCallback, useEffect, useState } from "react"
import {
  rvcLiveStart,
  rvcLiveStatus,
  rvcLiveStop,
  rvcTrainStart,
  rvcTrainStatus,
  type RvcLiveStatus,
  type RvcTrainStatus,
} from "@/api/client"

export function useLive() {
  const [liveStatus, setLiveStatus] = useState<RvcLiveStatus | null>(null)
  const [trainStatus, setTrainStatus] = useState<RvcTrainStatus | null>(null)
  const [modelOk, setModelOk] = useState(false)
  const [datasetCount, setDatasetCount] = useState(0)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [training, setTraining] = useState(false)
  const [message, setMessage] = useState("")

  const refresh = useCallback(async () => {
    try {
      const [ls, ts] = await Promise.all([rvcLiveStatus(), rvcTrainStatus()])
      setLiveStatus(ls)
      setModelOk(ls.model_ok)
      setDatasetCount(ls.dataset_count)
      setTrainStatus(ts)
    } catch {
      /* 后端未启动时静默 */
    }
  }, [])

  useEffect(() => {
    void refresh()
    // 训练/实时进行中 1.5s 快轮询，空闲 5s 慢轮询
    const busy = trainStatus?.running || liveStatus?.live_running
    const timer = window.setInterval(refresh, busy ? 1500 : 5000)
    return () => window.clearInterval(timer)
  }, [refresh, trainStatus?.running, liveStatus?.live_running])

  const start = useCallback(async () => {
    setStarting(true)
    setMessage("")
    try {
      const r = await rvcLiveStart()
      setMessage(r.hint ?? (r.already_running ? "实时变声已在运行中。" : "已启动。"))
    } catch (error) {
      setMessage(`启动失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setStarting(false)
      void refresh()
    }
  }, [refresh])

  const stop = useCallback(async () => {
    setStopping(true)
    setMessage("")
    try {
      const r = await rvcLiveStop()
      if (!r.ok) {
        setMessage(`停止失败：${r.error ?? "未知错误"}。若无法恢复，请点「一键恢复音频」或重启电脑。`)
        return
      }
      setMessage(
        r.note
          ? `${r.note}。注意：已打开的微信/游戏等应用不会自动切换录音设备，请退出后重新打开即可恢复。`
          : "已停止实时变声并还原声卡。注意：已打开的微信/游戏等应用不会自动跟随设备切换，请退出后重新打开即可恢复。",
      )
    } catch (error) {
      setMessage(`停止失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setStopping(false)
      void refresh()
    }
  }, [refresh])

  const train = useCallback(async () => {
    setTraining(true)
    setMessage("")
    try {
      const r = await rvcTrainStart()
      setMessage(r.already_running ? "训练已在后台进行中，下方将实时显示进度。" : "已启动训练，下方会实时显示各阶段进度（另开了控制台窗口可看完整日志）。")
    } catch (error) {
      setMessage(`训练启动失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setTraining(false)
      void refresh()
    }
  }, [refresh])

  const liveOn = Boolean(liveStatus?.live_running)

  return {
    liveStatus,
    liveOn,
    modelOk,
    trainStatus,
    trainingRunning: Boolean(trainStatus?.running),
    datasetCount,
    starting,
    stopping,
    training,
    message,
    start,
    stop,
    train,
  }
}
