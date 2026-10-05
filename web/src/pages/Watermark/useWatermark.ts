import { useCallback, useEffect, useRef, useState } from "react"
import {
  watermarkAutoSet,
  watermarkDetect,
  watermarkDownload,
  watermarkEmbed,
  watermarkStatus,
  type WatermarkDetectResult,
  type WatermarkEmbedResult,
  type WatermarkStatus,
} from "@/api/client"

/**
 * 「音频水印」页的状态与动作。
 *
 * 状态机只有两条轮询线：
 * 1. **权重下载**：下载中每 2s 拉一次 status（含两文件的字节进度），装好即停；
 * 2. **自动加标识码**：只在挂载时拉一次（开关是低频配置，不值得常轮询）。
 *
 * embed/detect 是用户动作，跑完即止不轮询 —— AudioSeal 在 CPU 上是亚秒级，无需作业票。
 */
export function useWatermark() {
  const [status, setStatus] = useState<WatermarkStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState("")
  const [feedback, setFeedback] = useState("")

  // —— 植入 ——
  const [embedFile, setEmbedFile] = useState<File | null>(null)
  const [code, setCode] = useState("")
  const [embedding, setEmbedding] = useState(false)
  const [embedResult, setEmbedResult] = useState<WatermarkEmbedResult | null>(null)

  // —— 检测 ——
  const [detectFile, setDetectFile] = useState<File | null>(null)
  const [detecting, setDetecting] = useState(false)
  const [detectResult, setDetectResult] = useState<WatermarkDetectResult | null>(null)

  // —— 自动加标识 ——
  const [autoEnabled, setAutoEnabled] = useState(false)
  const [autoCode, setAutoCode] = useState("")
  const [autoSaving, setAutoSaving] = useState(false)

  /** 下载轮询的存活凭证：卸载/装好时置 false，防止 setState 到已卸载组件。 */
  const pollRef = useRef(true)

  const refresh = useCallback(async (): Promise<WatermarkStatus | null> => {
    try {
      const st = await watermarkStatus()
      setStatus(st)
      setAutoEnabled(st.auto.enabled)
      setAutoCode(st.auto.code)
      setError("")
      return st
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  // 挂载拉一次；下载中则持续轮询到装好（或失败/取消）为止。
  useEffect(() => {
    pollRef.current = true
    void refresh()
    return () => {
      pollRef.current = false
    }
  }, [refresh])

  useEffect(() => {
    const dl = status?.download
    const busy =
      !!dl &&
      (dl.generator?.status === "downloading" || dl.detector?.status === "downloading")
    if (!busy || !pollRef.current) return
    const t = setInterval(() => void refresh(), 2000)
    return () => clearInterval(t)
  }, [status, refresh])

  const startDownload = useCallback(async () => {
    setStarting(true)
    setError("")
    try {
      await watermarkDownload()
      await refresh()
      setFeedback("权重下载已开始，下方可见进度")
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }, [refresh])

  /** 换文件即清旧结果 —— 结果属于上一份文件，留着会误导。 */
  const changeEmbedFile = useCallback((f: File | null) => {
    setEmbedFile(f)
    setEmbedResult(null)
    setFeedback("")
  }, [])

  const changeDetectFile = useCallback((f: File | null) => {
    setDetectFile(f)
    setDetectResult(null)
    setFeedback("")
  }, [])

  const embed = useCallback(async () => {
    if (!embedFile) return
    setEmbedding(true)
    setError("")
    setEmbedResult(null)
    try {
      const res = await watermarkEmbed({ file: embedFile }, code.trim())
      setEmbedResult(res)
      setFeedback(`已植入溯源码 ${res.code}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setEmbedding(false)
    }
  }, [embedFile, code])

  const detect = useCallback(async () => {
    if (!detectFile) return
    setDetecting(true)
    setError("")
    setDetectResult(null)
    try {
      const res = await watermarkDetect({ file: detectFile })
      setDetectResult(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDetecting(false)
    }
  }, [detectFile])

  const saveAuto = useCallback(
    async (enabled: boolean, nextCode: string) => {
      setAutoSaving(true)
      setError("")
      try {
        const res = await watermarkAutoSet(enabled, nextCode.trim())
        setAutoEnabled(res.enabled)
        setAutoCode(res.code)
        setFeedback(enabled ? `自动加标识已开启，溯源码 ${res.code}` : "自动加标识已关闭")
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setAutoSaving(false)
      }
    },
    [refresh]
  )

  return {
    // 状态
    status,
    loading,
    starting,
    error,
    feedback,
    // 植入
    embedFile,
    changeEmbedFile,
    code,
    setCode,
    embedding,
    embedResult,
    embed,
    // 检测
    detectFile,
    changeDetectFile,
    detecting,
    detectResult,
    detect,
    // 自动加标识
    autoEnabled,
    autoCode,
    autoSaving,
    saveAuto,
    refresh,
    startDownload,
  }
}
