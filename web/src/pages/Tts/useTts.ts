import { useCallback, useEffect, useState } from "react"
import { mediaUrl, purgeSession, saveSession, sendTts, ttsChainCheck } from "@/api/client"
import type { TtsChainInfo } from "@/types"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

export const TTS_MAX_LENGTH = 300
// 会话内保留多少条试听结果。**不落 localStorage**：产物在 outputs/.session/，
// 退出即删 —— 存下来的 url 下次打开必然是 404（这就是这里改掉 localStorage 的原因）。
export const TTS_HISTORY_LIMIT = 5
// 长文分段 ICL 的段长（字符），>0 启用分段，见 worker VM_TTS_SEG_CHARS
export const TTS_SEG_CHARS = 60

export type TtsLanguage = "zh" | "en"

export interface TtsHistoryItem {
  id: string
  /** 裸文件名（`tts_x.wav`）—— 保存/删除接口都按裸名定位，见后端 session_out.py */
  wav: string
  text: string
  language: TtsLanguage
  url: string
  createdAt: number
  /** 是否已存进作品库。保存过之后再点「保存」是幂等的，但仍然要能看出来 */
  saved: boolean
  /** 保存进行中/失败提示 */
  saving?: boolean
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
  const [saveMessage, setSaveMessage] = useState("")
  const [ttsHistory, setTtsHistory] = useState<TtsHistoryItem[]>([])

  const setTextLanguage = useCallback((value: string) => {
    setTextLanguageState(value === "en" ? "en" : "zh")
  }, [])

  const toggleAdvanced = useCallback(() => setAdvancedOpen((open) => !open), [])

  /**
   * 把音效标记插进文字（`[爆炸]`）—— 给「插音效」工具栏用的。
   *
   * `offset < 0` 表示"插在末尾"（调用方拿不到光标位置时的兜底；例如工具栏被点了
   * 但 textarea 还没聚焦过）。插完在片段后补一个空格：中文里 `[爆炸]那个`
   * 与 `[爆炸] 那个` 解析结果一样，但后者读起来更像人写的，也方便用户接着改。
   *
   * ⚠️ **越界要夹住**：`offset` 是渲染层给的，而 `text` 可能已经变了（用户打字与
   * 点击之间有一帧）。直接 `slice(0, offset)` 在 offset 过大时会把片段插到意外位置。
   */
  const insertMark = useCallback((snippet: string, offset = -1) => {
    setText((current) => {
      const at = offset < 0 || offset > current.length ? current.length : offset
      const before = current.slice(0, at)
      const after = current.slice(at)
      // 前面已经有空白（或本来就在行首）就不补前导空格，免得留出双空格
      const lead = before === "" || /\s$/.test(before) ? "" : " "
      return `${before}${lead}${snippet} ${after}`
    })
  }, [])

  const generate = useCallback(async () => {
    const trimmed = text.trim()
    if (!backendUp || synthesizing || !trimmed || trimmed.length > TTS_MAX_LENGTH) return
    if (!selectedVoiceId) {
      setErrorMessage("还没有可用音色，请先到「音色库」挖掘并保存一个音色。")
      return
    }
    setSynthesizing(true)
    setErrorMessage("")
    setSaveMessage("")
    setSlowHint("")
    const slowTimer = window.setTimeout(() => setSlowHint("正在合成…首次使用需加载语音模型，可能要等一两分钟"), SLOW_HINT_MS)
    try {
      const result = await sendTts(trimmed, textLanguage, selectedVoiceId,
                                   styleRefVoice || undefined,
                                   styleRefVoice ? TTS_SEG_CHARS : 0)
      // 后端给的是裸名或 `.session/x.wav`；存裸名，播放地址另算。
      const wav = result.url.split("/").pop() ?? ""
      const item: TtsHistoryItem = {
        id: `${Date.now()}`,
        wav,
        text: trimmed,
        language: textLanguage,
        url: mediaUrl(result.url),
        createdAt: Date.now(),
        saved: false,
      }
      setTtsHistory((current) => [item, ...current].slice(0, TTS_HISTORY_LIMIT))
    } catch (error) {
      setErrorMessage(friendlyError(error, "合成失败"))
    } finally {
      window.clearTimeout(slowTimer)
      setSlowHint("")
      setSynthesizing(false)
    }
  }, [text, textLanguage, backendUp, synthesizing, selectedVoiceId, styleRefVoice])

  /** 把某条会话产物存进作品库（复制到 outputs 根 + 登记历史）。幂等。 */
  const saveItem = useCallback(async (id: string) => {
    const target = ttsHistory.find((h) => h.id === id)
    if (!target || target.saved || target.saving) return
    setSaveMessage("")
    setTtsHistory((current) => current.map((h) => (h.id === id ? { ...h, saving: true } : h)))
    try {
      await saveSession(target.wav, {
        kind: "tts",
        voiceId: selectedVoiceId ?? "",
        inputText: target.text,
      })
      setTtsHistory((current) => current.map((h) => (h.id === id ? { ...h, saved: true, saving: false } : h)))
      setSaveMessage("已保存到作品库")
    } catch (error) {
      setTtsHistory((current) => current.map((h) => (h.id === id ? { ...h, saving: false } : h)))
      setSaveMessage(friendlyError(error, "保存失败"))
    }
  }, [ttsHistory, selectedVoiceId])

  /**
   * 从列表里去掉一条试听记录（**只影响列表，不删文件**）。
   *
   * 为什么不删文件：产物在 `.session/` 里，本来就"退出即删"，没有"删它"的必要；
   * 而一旦保存过，文件已经连同作品库记录一起在 outputs 根 —— 那时该去「作品库」删，
   * 在这里删会和历史记录脱节（留下指向不存在文件的记录）。
   */
  const discardItem = useCallback((id: string) => {
    setTtsHistory((current) => current.filter((h) => h.id !== id))
  }, [])

  /** 清空本次会话的全部产物（用户主动「清空本次」，退出时也会自动清）。 */
  const clearSession = useCallback(async () => {
    try {
      await purgeSession()
    } catch {
      /* 后端没起也不该拦住界面：本地列表照清 */
    }
    setTtsHistory([])
    setSaveMessage("")
  }, [])

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
    insertMark,
    styleRefVoice,
    setStyleRefVoice,
    synthesizing,
    slowHint,
    errorMessage,
    canGenerate,
    generate,
    ttsHistory,
    latestResult,
    saveMessage,
    saveItem,
    discardItem,
    clearSession,
    voices,
    selectedVoiceId,
    selectVoice,
    chain,
    chainLoading,
    runChainCheck,
  }
}
