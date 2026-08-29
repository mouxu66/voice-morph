import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { BASE, getMineState, getPipelineStatus, listVoices, mediaUrl, minePreview, mineRun, mineSave, runPipeline, uploadVideo, type MineState } from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"
import type { VoiceInfo } from "@/types"

export function useVoices() {
  const { backendUp, voices, setVoices, selectedVoiceId, selectVoice, selectedClips, clips, clearSelectedClips } = useAppStore()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [voiceIdInput, setVoiceIdInput] = useState("")
  const [errorMessage, setErrorMessage] = useState("")
  const [feedback, setFeedback] = useState("")
  // 挖掘流程状态
  const [mine, setMine] = useState<MineState>({ running: false, stage: "idle", message: "", kept: 0, clusters: [] })
  const [previewing, setPreviewing] = useState<string>("")   // 正在试听的切片名
  const [preview, setPreview] = useState<{ clip: string; url: string; text: string } | null>(null)
  const mineTimer = useRef<number | null>(null)
  // 挖掘参数（阈值默认 0.5；调高挖出更多不同音色）
  const [mineSim, setMineSim] = useState(0.5)
  const [mineMinCluster, setMineMinCluster] = useState(1)
  // 批量导入文件夹：上传 -> 解析切片 -> 自动挖掘
  const [importing, setImporting] = useState(false)
  const [importMessage, setImportMessage] = useState("")

  const loadVoices = useCallback(async () => {
    setLoading(true); setErrorMessage("")
    try { setVoices(await listVoices()) }
    catch (error) { setErrorMessage(friendlyError(error, "音色加载失败")) }
    finally { setLoading(false) }
  }, [setVoices])
  useEffect(() => { void loadVoices() }, [loadVoices, backendUp])

  // 页面打开时同步后端已有挖掘结果（刷新后候选不丢）
  useEffect(() => {
    if (!backendUp) return
    getMineState().then(setMine).catch(() => { /* 后端未启动时忽略 */ })
  }, [backendUp])

  // 挖掘状态轮询（running 时每 3s，结束后再拉一次即停）
  useEffect(() => {
    if (!backendUp) return
    if (!mine.running) return
    mineTimer.current = window.setInterval(async () => {
      try {
        const s = await getMineState()
        setMine(s)
      } catch { /* 忽略轮询错误 */ }
    }, 3000)
    return () => { if (mineTimer.current) window.clearInterval(mineTimer.current) }
  }, [mine.running, backendUp])

  const startMine = useCallback(async () => {
    setErrorMessage(""); setFeedback(""); setPreview(null)
    try {
      setMine((s) => ({ ...s, running: true, stage: "running", message: "正在转写与提取声纹…" }))
      await mineRun({ sim_threshold: mineSim, min_cluster_size: mineMinCluster })
    } catch (error) {
      setMine((s) => ({ ...s, running: false, stage: "error", message: friendlyError(error, "挖掘启动失败") }))
    }
  }, [mineSim, mineMinCluster])

  // 批量导入文件夹：webkitdirectory 选择 → 逐个上传 → 解析切片（管线）→ 自动按当前参数挖掘
  const importFolder = useCallback(async (files: File[] | undefined) => {
    const media = (files ?? []).filter((f) => /\.(mp4|mkv|mov|flv|webm|avi|wav|mp3|m4a|flac|ogg|aac|wma)$/i.test(f.name))
    if (!media.length) { setErrorMessage("所选文件夹里没有可用的音频/视频文件"); return }
    setImporting(true)
    setErrorMessage(""); setFeedback(""); setPreview(null)
    try {
      for (let i = 0; i < media.length; i++) {
        setImportMessage(`上传素材 ${i + 1}/${media.length}：${media[i].name}`)
        try {
          await uploadVideo(media[i])
        } catch (e) {
          const msg = friendlyError(e, "上传失败")
          if (!/同名文件已存在/.test(msg)) throw new Error(`${media[i].name}: ${msg}`)
        }
      }
      setImportMessage("解析素材（提取人声 → 切片）…")
      await runPipeline()
      for (;;) {
        await new Promise((r) => setTimeout(r, 3000))
        const s = await getPipelineStatus()
        if (s.step) setImportMessage(`解析素材：${s.step} ${s.percent ?? 0}%`)
        if (!s.running) {
          if (s.error) throw new Error(s.error)
          break
        }
      }
      setImportMessage("开始音色挖掘…")
      setMine((s) => ({ ...s, running: true, stage: "running", message: "正在转写与提取声纹…" }))
      await mineRun({ sim_threshold: mineSim, min_cluster_size: mineMinCluster })
      setImportMessage("")
      setFeedback(`已导入 ${media.length} 个文件，切片完成，正在挖掘音色`)
    } catch (error) {
      setImportMessage("")
      setErrorMessage(friendlyError(error, "批量导入失败"))
    } finally {
      setImporting(false)
    }
  }, [mineSim, mineMinCluster])

  const selectedDuration = useMemo(() =>
    clips.filter((clip) => selectedClips.has(clip.name)).reduce((sum, clip) => sum + clip.duration_s, 0),
    [clips, selectedClips])

  const createVoice = async () => {
    const id = voiceIdInput.trim()
    if (!id) { setErrorMessage("请输入音色 ID"); return }
    if (!selectedClips.size) { setErrorMessage("请先在音色工坊采纳片段"); return }
    setBusy(true); setErrorMessage(""); setFeedback("")
    try {
      const body = [...selectedClips].map((clip) => `clips=${encodeURIComponent(clip)}`).join("&")
      const response = await fetch(`${BASE}/voicebank?voice_id=${encodeURIComponent(id)}`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || response.statusText)
      }
      const data = await response.json() as { duration_s: number }
      clearSelectedClips()
      setVoiceIdInput("")
      setFeedback(`「${id}」已创建，参考音频 ${data.duration_s.toFixed(1)} 秒`)
      await loadVoices()
      selectVoice(id)
    } catch (error) {
      setErrorMessage(friendlyError(error, "创建音色失败"))
    } finally {
      setBusy(false)
    }
  }

  const deleteVoice = async (voice: VoiceInfo) => {
    if (!window.confirm(`确定删除音色「${voice.id}」吗？`)) return
    try {
      const response = await fetch(`${BASE}/voicebank/${encodeURIComponent(voice.id)}`, { method: "DELETE" })
      if (!response.ok) throw new Error("删除失败")
      if (selectedVoiceId === voice.id) selectVoice("")
      await loadVoices()
    } catch (error) {
      setErrorMessage(friendlyError(error, "删除音色失败"))
    }
  }

  // 试听候选：用代表切片合成一句与视频无关的新文本
  const tryPreview = useCallback(async (clip: string) => {
    setPreviewing(clip); setErrorMessage("")
    try {
      const r = await minePreview(clip)
      setPreview({ clip, url: mediaUrl(r.url), text: r.text })
    } catch (error) {
      setErrorMessage(friendlyError(error, "试听合成失败"))
    } finally {
      setPreviewing("")
    }
  }, [])

  // 保存候选为正式音色（含同簇成员），成功后自动选中并刷新
  const saveCandidate = useCallback(async (clip: string, members: string[]) => {
    const id = voiceIdInput.trim() || clip.slice(-8)
    setBusy(true); setErrorMessage("")
    try {
      const r = await mineSave(clip, id, id, members)
      setFeedback(`音色「${id}」已保存（${r.duration_s.toFixed(1)}s / ${r.clips} 段），已设为当前音色`)
      setVoiceIdInput("")
      await loadVoices()
      selectVoice(id)
    } catch (error) {
      setErrorMessage(friendlyError(error, "保存音色失败"))
    } finally {
      setBusy(false)
    }
  }, [voiceIdInput, loadVoices, selectVoice])

  return {
    backendUp, voices, selectedVoiceId, selectVoice, selectedClips, selectedDuration, loading, busy,
    voiceIdInput, setVoiceIdInput, errorMessage, feedback, loadVoices, createVoice, deleteVoice,
    audioUrl: (voice: VoiceInfo) => mediaUrl(`/media/voicebank/${voice.id}/${voice.reference}`),
    mine, startMine, previewing, preview, tryPreview, saveCandidate,
    mineSim, setMineSim, mineMinCluster, setMineMinCluster,
    importing, importMessage, importFolder,
  }
}
