import { useCallback, useEffect, useState } from "react"
import { mediaUrl, sendTts, ttsChainCheck } from "@/api/client"
import type { TtsChainInfo } from "@/types"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"
import { loadHistory, prependHistory, STORAGE_KEYS } from "@/lib/history"

export const TTS_MAX_LENGTH = 300
export const TTS_HISTORY_LIMIT = 5
// 长文分段 ICL 的段长（字符），>0 启用分段，见 worker VM_TTS_SEG_CHARS
export const TTS_SEG_CHARS = 60

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
  // 风格参考 ICL：""=不启用；非空=用该音色 reference 作风格参考并走长文分段
  const [styleRefVoice, setStyleRefVoice] = useState("")
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
      const result = await sendTts(trimmed, textLanguage, selectedVoiceId,
                                   styleRefVoice || undefined,
                                   styleRefVoice ? TTS_SEG_CHARS : 0)
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
  }, [text, textLanguage, backendUp, synthesizing, selectedVoiceId, styleRefVoice])

  const textLength = text.length
  const overLimit = textLength > TTS_MAX_LENGTH
  const canGenerate = backendUp && !synthesizing && text.trim().length > 0 && !overLimit && Boolean(selectedVoiceId)
  const latestResult = ttsHistory[0] ?? null

  // ---- 输字变声链路自检（进页面自动跑一遍）----
  // 首页 STEP 0 首推就是这条链路，但此前没有任何自检兜底 —— 用户被推荐去的那条路，
  // 反而是故障时最没指引的那条。这里对齐实时变声页的 SEND CHAIN 形态。
  const [chain, setChain] = useState<TtsChainInfo | null>(null)
  const [chainLoading, setChainLoading] = useState(false)

  const runChainCheck = useCallback(async () => {
    setChainLoading(true)
    try {
      setChain(await ttsChainCheck())
    } catch {
      /* 后端未启动时静默，与全局轮询一致 */
    } finally {
      setChainLoading(false)
    }
  }, [])

  // 挂载后自动查一次；合成过程中不打扰（合成本身就在验证链路）。
  // 注意别做成高频轮询：本接口每次要枚举模型目录 + 真写一次探针，不是零成本。
  useEffect(() => {
    if (synthesizing || chain !== null) return
    void runChainCheck()
  }, [synthesizing, chain, runChainCheck])

  // 合成成功后链路显然已通，把陈旧的红项清掉（否则修好了卡片还挂着旧问题）
  useEffect(() => {
    if (latestResult) void runChainCheck()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [latestResult?.id])

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
    styleRefVoice,
    setStyleRefVoice,
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
    chain,
    chainLoading,
    runChainCheck,
  }
}
