import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  rvcLiveStart,
  rvcLiveStatus,
  rvcLiveStop,
  rvcLiveReset,
  rvcTrainStart,
  rvcTrainStatus,
  type KangarooLiveStatus,
} from "@/api/client"

export function useKangaroo() {
  const navigate = useNavigate()
  const [script, setScript] = useState("")
  const [liveStatus, setLiveStatus] = useState<KangarooLiveStatus | null>(null)
  const [trainingRunning, setTrainingRunning] = useState(false)
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
      setTrainingRunning(ts.train_running)
    } catch {
      /* 后端未启动时静默 */
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(refresh, 3000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const toTts = useCallback(() => {
    const s = script.trim()
    if (!s) return
    navigate(`/tts?text=${encodeURIComponent(s)}`)
  }, [script, navigate])

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

  const reset = useCallback(async () => {
    setMessage("")
    try {
      const r = await rvcLiveReset()
      if (!r.ok) {
        setMessage(`恢复音频设备失败：${r.error ?? "未知错误"}。可到 Windows 声音设置中手动选择默认设备，或重启电脑。`)
        return
      }
      setMessage("已把音频设备恢复为默认（真实扬声器/麦克风）。注意：已打开的微信/游戏等应用不会自动跟随设备切换，请退出后重新打开即可恢复。")
    } catch (error) {
      setMessage(`恢复失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      void refresh()
    }
  }, [refresh])

  const train = useCallback(async () => {
    setTraining(true)
    setMessage("")
    try {
      const r = await rvcTrainStart()
      setMessage(r.already_running ? "训练已在后台进行中（请在新弹出的控制台窗口查看进度）。" : "已在新窗口启动训练，结束后自动生成模型。")
    } catch (error) {
      setMessage(`训练启动失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setTraining(false)
      void refresh()
    }
  }, [refresh])

  const liveOn = Boolean(liveStatus?.live_running)

  return {
    script,
    setScript,
    toTts,
    liveStatus,
    liveOn,
    modelOk,
    trainingRunning,
    datasetCount,
    starting,
    stopping,
    training,
    message,
    start,
    stop,
    reset,
    train,
  }
}