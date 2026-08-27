import { useCallback, useState } from "react"
import { mediaUrl, sendTts } from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"
import { loadHistory, prependHistory, STORAGE_KEYS } from "@/lib/history"

export const TTS_MAX_LENGTH = 300
export const TTS_HISTORY_LIMIT = 5

export type TtsLanguage = "zh" | "en"

export interface TtsHistoryItem {
  id: string
  text: string
  language: TtsLanguage
  url: string
  createdAt: number
}

const SLOW_HINT_MS = 8000

export function useTts() {
  const { backendUp, voices, selectedVoiceId, selectVoice } = useAppStore()
  const [text, setText] = useState("")
  const [textLanguage, setTextLanguageState] = useState<TtsLanguage>("zh")
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [synthesizing, setSynthesizing] = useState(false)
  const [slowHint, setSlowHint] = useState("")
  const [errorMessage, setErrorMessage] = useState("")
  const [ttsHistory, setTtsHistory] = useState<TtsHistoryItem[]>(() =>
    loadHistory<TtsHistoryItem>(STORAGE_KEYS.ttsHistory, TTS_HISTORY_LIMIT))

  const setTextLanguage = useCallback((value: string) => {
    setTextLanguageState(value === "en" ? "en" : "zh")
  }, [])

  const toggleAdvanced = useCallback(() => setAdvancedOpen((open) => !open), [])

  const generate = useCallback(async () => {
    const trimmed = text.trim()
    if (!backendUp || synthesizing || !trimmed || trimmed.length > TTS_MAX_LENGTH) return
    if (!selectedVoiceId) {
      setErrorMessage("还没有可用音色，请先到「音色库」挖掘并保存一个音色。")
      return
    }
    setSynthesizing(true)
    setErrorMessage("")
    setSlowHint("")
    const slowTimer = window.setTimeout(() => setSlowHint("正在合成…首次使用需加载语音模型，可能要等一两分钟"), SLOW_HINT_MS)
    try {
      const result = await sendTts(trimmed, textLanguage, selectedVoiceId)
      const item: TtsHistoryItem = {
        id: `${Date.now()}`,
        text: trimmed,
        language: textLanguage,
        url: mediaUrl(result.url),
        createdAt: Date.now(),
      }
      setTtsHistory((current) => prependHistory(STORAGE_KEYS.ttsHistory, current, item, TTS_HISTORY_LIMIT))
    } catch (error) {
      setErrorMessage(friendlyError(error, "合成失败"))
    } finally {
      window.clearTimeout(slowTimer)
      setSlowHint("")
      setSynthesizing(false)
    }
  }, [text, textLanguage, backendUp, synthesizing, selectedVoiceId])

  const textLength = text.length
  const overLimit = textLength > TTS_MAX_LENGTH
  const canGenerate = backendUp && !synthesizing && text.trim().length > 0 && !overLimit && Boolean(selectedVoiceId)
  const latestResult = ttsHistory[0] ?? null

  return {
    backendUp,
    text,
    setText,
    textLength,
    overLimit,
    textLanguage,
    setTextLanguage,
    advancedOpen,
    toggleAdvanced,
    synthesizing,
    slowHint,
    errorMessage,
    canGenerate,
    generate,
    ttsHistory,
    latestResult,
    voices,
    selectedVoiceId,
    selectVoice,
  }
}
