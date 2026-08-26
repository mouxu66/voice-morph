import { useCallback, useEffect, useMemo, useState } from "react"
import { BASE, listVoices, mediaUrl } from "@/api/client"
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

  const loadVoices = useCallback(async () => {
    setLoading(true); setErrorMessage("")
    try { setVoices(await listVoices()) }
    catch (error) { setErrorMessage(friendlyError(error, "音色加载失败")) }
    finally { setLoading(false) }
  }, [setVoices])
  useEffect(() => { void loadVoices() }, [loadVoices, backendUp])

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

  return {
    backendUp, voices, selectedVoiceId, selectVoice, selectedClips, selectedDuration, loading, busy,
    voiceIdInput, setVoiceIdInput, errorMessage, feedback, loadVoices, createVoice, deleteVoice,
    audioUrl: (voice: VoiceInfo) => mediaUrl(`/media/voicebank/${voice.id}/${voice.reference}`),
  }
}
