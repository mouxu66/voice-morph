import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { BASE, getMineState, getPipelineStatus, listVoices, mediaUrl, minePreview, mineRun, mineSave, runPipeline, uploadVideo, type MineState } from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"
import type { VoiceInfo } from "@/types"

/**
 * 音色库页的状态与动作。
 *
 * `mineOn` / `workshopOn` 来自能力清单（C 类门控，2026-09-21 补）：
 * 「音色挖掘」与「音色工坊」都是**可关**能力（`sound.mine` / `sound.workshop`），
 * 但它们的 UI 长在**核心页** `core.voices`（恒注册）上。关掉插件后端点不再挂载，
 * 界面却照旧渲染 → 用户点一下就 404。所以这里比照 `useAudiobook(enabled)` 的写法
 * 收两个开关：关掉时**连轮询都不发**（端点不存在，轮询只是空转刷 404）。
 *
 * 两个开关是分开的，因为依赖不同：
 *   · `mineOn`      → `mineRun/minePreview/mineSave/getMineState`（只在已有切片上挖）
 *   · `workshopOn`  → `runPipeline/getPipelineStatus`（上传素材后要跑流水线切片）
 * 「导入素材 / 文件夹 / 录音」这三条路都要先切片再挖，所以同时要 `workshopOn`；
 * 而「开始挖掘」直接吃已有切片，只要 `mineOn`。返回值里的 `ingestOn` 就是这个与。
 */
export function useVoices(mineOn = true, workshopOn = true) {
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
  // 麦克风录音：MediaRecorder 录完自动进入 解析+挖掘 流程
  const [recording, setRecording] = useState(false)
  const [recordSeconds, setRecordSeconds] = useState(0)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const recordTimerRef = useRef<number | null>(null)
  const recordChunksRef = useRef<Blob[]>([])

  const loadVoices = useCallback(async () => {
    setLoading(true); setErrorMessage("")
    try { setVoices(await listVoices()) }
    catch (error) { setErrorMessage(friendlyError(error, "音色加载失败")) }
    finally { setLoading(false) }
  }, [setVoices])
  useEffect(() => { void loadVoices() }, [loadVoices, backendUp])

  // 页面打开时同步后端已有挖掘结果（刷新后候选不丢）
  useEffect(() => {
    if (!mineOn || !backendUp) return
    getMineState().then(setMine).catch(() => { /* 后端未启动时忽略 */ })
  }, [mineOn, backendUp])

  // 挖掘状态轮询（running 时每 3s，结束后再拉一次即停）
  useEffect(() => {
    if (!mineOn || !backendUp) return
    if (!mine.running) return
    mineTimer.current = window.setInterval(async () => {
      try {
        const s = await getMineState()
        setMine(s)
      } catch { /* 忽略轮询错误 */ }
    }, 3000)
    return () => { if (mineTimer.current) window.clearInterval(mineTimer.current) }
  }, [mineOn, mine.running, backendUp])

  const startMine = useCallback(async () => {
    if (!mineOn) return
    setErrorMessage(""); setFeedback(""); setPreview(null)
    try {
      setMine((s) => ({ ...s, running: true, stage: "running", message: "正在转写与提取声纹…" }))
      await mineRun({ sim_threshold: mineSim, min_cluster_size: mineMinCluster })
    } catch (error) {
      setMine((s) => ({ ...s, running: false, stage: "error", message: friendlyError(error, "挖掘启动失败") }))
    }
  }, [mineOn, mineSim, mineMinCluster])

  // 多渠道音源共用流程：上传素材 → 解析切片 → 自动按当前参数挖掘
  const ingestFiles = useCallback(async (media: File[], verbLabel: string) => {
    if (!mineOn || !workshopOn) return
    setImporting(true)
    setErrorMessage(""); setFeedback(""); setPreview(null)
    try {
      for (let i = 0; i < media.length; i++) {
        setImportMessage(`上传素材 ${i + 1}/${media.length}：${media[i].name}`)
        try {
          await uploadVideo(media[i])
        } catch (e) {
          const msg = friendlyError(e, "上传失败")
          // 带上 `cause`：friendlyError 只留下一句给人看的话，原始异常（网络/服务端细节）
          // 否则就永远丢了，上层想排查也无处下手。
          if (!/同名文件已存在/.test(msg)) throw new Error(`${media[i].name}: ${msg}`, { cause: e })
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
      setFeedback(`已${verbLabel} ${media.length} 个文件，切片完成，正在挖掘音色`)
    } catch (error) {
      setImportMessage("")
      setErrorMessage(friendlyError(error, "导入失败"))
    } finally {
      setImporting(false)
    }
  }, [mineOn, workshopOn, mineSim, mineMinCluster])

  // 批量导入文件夹：webkitdirectory 选择 → 共用导入流程
  const importFolder = useCallback(async (files: File[] | undefined) => {
    const media = (files ?? []).filter((f) => /\.(mp4|mkv|mov|flv|webm|avi|wav|mp3|m4a|flac|ogg|aac|wma)$/i.test(f.name))
    if (!media.length) { setErrorMessage("所选文件夹里没有可用的音频/视频文件"); return }
    await ingestFiles(media, "导入")
  }, [ingestFiles])

  // 选择单个/多个音视频文件（音频文件、录音文件等，不限于文件夹）
  const importFiles = useCallback(async (files: File[] | undefined) => {
    const media = (files ?? []).filter((f) => /\.(mp4|mkv|mov|flv|webm|avi|wav|mp3|m4a|flac|ogg|aac|wma)$/i.test(f.name))
    if (!media.length) { setErrorMessage("没有可用的音频/视频文件"); return }
    await ingestFiles(media, "导入")
  }, [ingestFiles])

  // 开始麦克风录音：MediaRecorder 输出 audio/webm，直接当素材走现有上传链路
  const startRecording = useCallback(async () => {
    setErrorMessage(""); setFeedback("")
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : ""
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      recordChunksRef.current = []
      rec.ondataavailable = (e) => { if (e.data.size) recordChunksRef.current.push(e.data) }
      rec.start(1000)
      recorderRef.current = rec
      setRecording(true)
      setRecordSeconds(0)
      recordTimerRef.current = window.setInterval(() => setRecordSeconds((s) => s + 1), 1000)
    } catch (error) {
      setErrorMessage(friendlyError(error, "无法访问麦克风，请检查系统与浏览器麦克风权限"))
    }
  }, [])

  // 停止录音：合成 webm 文件（命名为 recording_时间戳），进入 解析+挖掘 流程
  const stopRecording = useCallback(async () => {
    const rec = recorderRef.current
    if (!rec) return
    if (recordTimerRef.current) { window.clearInterval(recordTimerRef.current); recordTimerRef.current = null }
    setRecording(false)
    const blob = await new Promise<Blob>((resolve) => {
      rec.onstop = () => resolve(new Blob(recordChunksRef.current, { type: rec.mimeType || "audio/webm" }))
      rec.stop()
    })
    rec.stream.getTracks().forEach((t) => t.stop())
    recorderRef.current = null
    if (blob.size < 1000) { setErrorMessage("录音太短，没有捕捉到内容"); return }
    const ext = (rec.mimeType || blob.type).includes("ogg") ? "ogg" : "webm"
    const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14)
    const file = new File([blob], `recording_${stamp}.${ext}`, { type: blob.type })
    await ingestFiles([file], "录入")
  }, [ingestFiles])

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
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(data.detail || `删除失败（HTTP ${response.status}）`)
      }
      if (selectedVoiceId === voice.id) selectVoice("")
      await loadVoices()
    } catch (error) {
      setErrorMessage(friendlyError(error, "删除音色失败"))
    }
  }

  // 试听候选：用代表切片合成一句与视频无关的新文本
  const tryPreview = useCallback(async (clip: string) => {
    if (!mineOn) return
    setPreviewing(clip); setErrorMessage("")
    try {
      const r = await minePreview(clip)
      setPreview({ clip, url: mediaUrl(r.url), text: r.text })
    } catch (error) {
      setErrorMessage(friendlyError(error, "试听合成失败"))
    } finally {
      setPreviewing("")
    }
  }, [mineOn])

  // 保存候选为正式音色（含同簇成员），成功后自动选中并刷新
  const saveCandidate = useCallback(async (clip: string, members: string[]) => {
    if (!mineOn) return
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
  }, [mineOn, voiceIdInput, loadVoices, selectVoice])

  return {
    backendUp, voices, selectedVoiceId, selectVoice, selectedClips, selectedDuration, loading, busy,
    voiceIdInput, setVoiceIdInput, errorMessage, feedback, loadVoices, createVoice, deleteVoice,
    audioUrl: (voice: VoiceInfo) => mediaUrl(`/media/voicebank/${voice.id}/${voice.reference}`),
    mineOn, ingestOn: mineOn && workshopOn,
    mine, startMine, previewing, preview, tryPreview, saveCandidate,
    mineSim, setMineSim, mineMinCluster, setMineMinCluster,
    importing, importMessage, importFolder, importFiles,
    recording, recordSeconds, startRecording, stopRecording,
  }
}
