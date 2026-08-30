import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  cancelPipeline,
  deleteRawVideo,
  getPipelineStatus,
  listClips,
  listRawVideos,
  mediaUrl,
  openFolder,
  runPipeline,
  uploadVideo,
  type PipelineStatus,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"
import type { ClipItem, VideoItem } from "@/types"

const IDLE_PIPELINE: PipelineStatus = {
  running: false, status: "idle", step: "", message: "", percent: 0, clips: 0, error: "",
}

export function useWorkshop() {
  const { backendUp, clips, setClips, selectedClips, toggleClip, clearSelectedClips } = useAppStore()
  const [videos, setVideos] = useState<VideoItem[]>([])
  const [loading, setLoading] = useState(true)
  const [errorMessage, setErrorMessage] = useState("")
  const [feedback, setFeedback] = useState("")
  const [qualityFilter, setQualityFilter] = useState<"全部" | "推荐" | "需检查">("全部")
  const [reviewMode, setReviewMode] = useState<"初筛" | "精审">("初筛")
  const [decisions, setDecisions] = useState<Record<string, "采纳" | "驳回">>({})

  // 流水线（后台运行 + 轮询进度）
  const [pipeline, setPipeline] = useState<PipelineStatus>(IDLE_PIPELINE)
  const pollRef = useRef<number | null>(null)

  // 素材上传
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [dragging, setDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const stopPolling = useCallback(() => {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [])

  useEffect(() => stopPolling, [stopPolling])

  const loadWorkshop = useCallback(async () => {
    setLoading(true)
    setErrorMessage("")
    try {
      const [videoItems, clipItems] = await Promise.all([listRawVideos(), listClips()])
      setVideos(videoItems)
      setClips(clipItems)
    } catch (error) {
      setErrorMessage(friendlyError(error, "素材加载失败"))
    } finally {
      setLoading(false)
    }
  }, [setClips])

  useEffect(() => { void loadWorkshop() }, [loadWorkshop, backendUp])

  const startPipeline = async () => {
    setErrorMessage("")
    setFeedback("")
    try {
      await runPipeline()
      setPipeline({ ...IDLE_PIPELINE, running: true, status: "running", percent: 1, message: "正在启动流水线…" })
      pollRef.current = window.setInterval(async () => {
        try {
          const st = await getPipelineStatus()
          setPipeline(st)
          if (!st.running && st.status !== "running") {
            stopPolling()
            if (st.status === "done") setFeedback(st.message)
            if (st.status === "error") setErrorMessage(st.error || "流水线出错")
            await loadWorkshop()
          }
        } catch (e) {
          stopPolling()
          setErrorMessage(friendlyError(e, "获取流水线进度失败"))
        }
      }, 1000)
    } catch (error) {
      setErrorMessage(friendlyError(error, "无法启动流水线"))
    }
  }

  const stopPipeline = async () => {
    try { await cancelPipeline() } catch { /* 忽略取消失败 */ }
    setFeedback("正在停止…")
  }

  const resetPipeline = useCallback(() => {
    stopPolling()
    setPipeline(IDLE_PIPELINE)
  }, [stopPolling])

  // 素材上传
  const handleFiles = async (files: FileList | null) => {
    if (!files || !files.length) return
    setUploading(true)
    setErrorMessage("")
    try {
      for (const file of Array.from(files)) {
        await uploadVideo(file, (p) => setUploadProgress(p))
      }
      setFeedback(`已上传 ${files.length} 个视频素材`)
      await loadWorkshop()
    } catch (error) {
      setErrorMessage(friendlyError(error, "上传失败"))
    } finally {
      setUploading(false)
      setUploadProgress(0)
    }
  }

  const openRawFolder = async () => {
    try { await openFolder("raw_videos") } catch (error) { setErrorMessage(friendlyError(error)) }
  }

  // 删除素材（联动清理切片/音轨等派生产物）。被音色引用时后端要求 force，音色本身不受影响。
  const deleteVideo = async (name: string, usedBy: string[]) => {
    setErrorMessage("")
    try {
      const res = await deleteRawVideo(name, usedBy.length > 0)
      setFeedback(usedBy.length
        ? `已删除素材（音色「${usedBy.join("、")}」不受影响）`
        : `已删除素材${res.clips ? `及 ${res.clips} 个切片` : ""}${res.related ? `、${res.related} 个中间文件` : ""}`)
      await loadWorkshop()
    } catch (error) {
      setErrorMessage(friendlyError(error, "删除素材失败"))
      // 前端展示的引用数据可能已过期（如别处刚完成一次克隆）：刷新列表拿到最新 used_by，
      // 卡片按钮会升级为「仍要删除」，用户确认后再点一次即可强删——避免 409 死循环
      await loadWorkshop()
    }
  }

  // RVC 训练集导出（带当前采纳片段）
  const exportRvc = async () => {
    setErrorMessage("")
    const query = selectedClips.size
      ? `?${[...selectedClips].map((c) => `clips=${encodeURIComponent(c)}`).join("&")}`
      : ""
    const url = (import.meta.env.DEV ? "" : "http://127.0.0.1:8000") + `/api/export/rvc${query}`
    try {
      const res = await fetch(url)
      if (!res.ok) throw new Error(`导出失败（${res.status}）`)
      const blob = await res.blob()
      const a = document.createElement("a")
      a.href = URL.createObjectURL(blob)
      a.download = "rvc_dataset.zip"
      a.click()
      URL.revokeObjectURL(a.href)
      setFeedback(`已导出 ${selectedClips.size || "全部"} 个片段为 RVC 训练集`)
    } catch (error) {
      setErrorMessage(friendlyError(error, "RVC 导出失败"))
    }
  }

  const selectedItems = useMemo(() => clips.filter((clip) => selectedClips.has(clip.name)), [clips, selectedClips])
  const selectedDuration = selectedItems.reduce((total, clip) => total + clip.duration_s, 0)
  const visibleClips = useMemo(() => clips.filter((clip) => {
    const quality = clip.loudness_dbfs >= -18 && clip.loudness_dbfs <= -8 ? "推荐" : "需检查"
    if (qualityFilter !== "全部" && quality !== qualityFilter) return false
    if (reviewMode === "精审" && decisions[clip.name]) return false
    return true
  }), [clips, decisions, qualityFilter, reviewMode])

  // 精审快捷键：A 采纳 / R 驳回
  const setDecision = useCallback((clipName: string, decision: "采纳" | "驳回") => {
    setDecisions((current) => ({ ...current, [clipName]: decision }))
    if (decision === "采纳" && !selectedClips.has(clipName)) toggleClip(clipName)
    if (decision === "驳回" && selectedClips.has(clipName)) toggleClip(clipName)
  }, [selectedClips, toggleClip])

  useEffect(() => {
    if (reviewMode !== "精审") return
    const onKey = (e: KeyboardEvent) => {
      const target = visibleClips.find((c) => !decisions[c.name])
      if (!target) return
      const key = e.key.toLowerCase()
      if (key === "a") { e.preventDefault(); setDecision(target.name, "采纳") }
      else if (key === "r") { e.preventDefault(); setDecision(target.name, "驳回") }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [reviewMode, visibleClips, decisions, setDecision])

  useEffect(() => {
    if (!feedback) return
    const t = window.setTimeout(() => setFeedback(""), 4000)
    return () => window.clearTimeout(t)
  }, [feedback])

  return {
    backendUp, videos, clips, visibleClips, loading, errorMessage, feedback, qualityFilter, setQualityFilter,
    reviewMode, setReviewMode, selectedClips, selectedItems, selectedDuration, decisions,
    pipeline, startPipeline, stopPipeline, resetPipeline,
    uploading, uploadProgress, dragging, setDragging, fileInputRef, handleFiles, openRawFolder, exportRvc,
    deleteVideo,
    loadWorkshop, toggleClip, clearSelectedClips, setDecision,
    clipAudioUrl: (clip: ClipItem) => mediaUrl(`/media/clips/${clip.name}.wav`),
  }
}
