import { useCallback, useEffect, useRef, useState } from "react"
import {
  cascadeStart,
  cascadeStatus,
  cascadeStop,
  listRvcVoices,
  listVoices,
  type CascadeStatus,
  type RvcVoice,
} from "@/api/client"
import type { VoiceInfo } from "@/types"

/**
 * 级联变声页的状态机：录音 → ASR → 文字 → TTS → 虚拟声卡。
 *
 * 与实时变声共用同一出口（CABLE），但走文字中转：输出只含目标音色，
 * 源说话人的口音与发音习惯完全不进入。音色取自音色库（TTS 参考音频），
 * 不需要 RVC 训练——这页解锁条件与实时变声不同，别套用 model_ready。
 *
 * worker 懒启动（约 30s）：start 返回 warming 后挂起，轮询发现
 * worker_ready 自动重试一次，用户无需重复点击。
 */
const LS_VOICE = "vm_cascade_voice"
const LS_MODE = "vm_cascade_mode"
const LS_CHUNK = "vm_cascade_chunk"
const LS_RVC_VOICE = "vm_cascade_rvc_voice"

type Feedback = { tone: "ok" | "error" | "info"; text: string }

function msgOf(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : String(error)
  return raw || fallback
}

function readLS(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeLS(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* 隐私模式下写不了，忽略 */
  }
}

export function useCascade() {
  const [voices, setVoices] = useState<VoiceInfo[]>([])
  const [selectedVoice, setSelectedVoice] = useState<string | null>(() => readLS(LS_VOICE))
  const [status, setStatus] = useState<CascadeStatus | null>(null)
  const [mode, setMode] = useState<"stream" | "whole">(() =>
    readLS(LS_MODE) === "whole" ? "whole" : "stream",
  )
  const [chunkMaxS, setChunkMaxS] = useState<number>(() => {
    const v = Number(readLS(LS_CHUNK))
    return Number.isFinite(v) && v >= 2 && v <= 15 ? v : 6
  })
  // 末尾是否接 RVC：null=不接（音色靠 TTS 克隆），否则为该 RVC 音色 ID
  const [rvcVoices, setRvcVoices] = useState<RvcVoice[]>([])
  const [rvcVoice, setRvcVoice] = useState<string | null>(() => readLS(LS_RVC_VOICE) || null)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const [transcripts, setTranscripts] = useState<{ id: number; text: string }[]>([])

  const pendingRetryRef = useRef(false)
  const lastChunksRef = useRef(0)

  useEffect(() => writeLS(LS_VOICE, selectedVoice ?? ""), [selectedVoice])
  useEffect(() => writeLS(LS_MODE, mode), [mode])
  useEffect(() => writeLS(LS_CHUNK, String(chunkMaxS)), [chunkMaxS])
  useEffect(() => writeLS(LS_RVC_VOICE, rvcVoice ?? ""), [rvcVoice])

  // RVC 音色清单只在进页面时拉一次（/rvc/voices 要遍历日志目录，不进 1s 轮询）
  useEffect(() => {
    let alive = true
    listRvcVoices()
      .then((r) => { if (alive) setRvcVoices(r.voices ?? []) })
      .catch(() => { /* 后端未启动时静默 */ })
    return () => { alive = false }
  }, [])

  // 选中的 RVC 音色被删掉后自动断开，避免带着无效 ID 启动
  useEffect(() => {
    if (!rvcVoices.length || !rvcVoice) return
    if (!rvcVoices.some((v) => v.id === rvcVoice)) setRvcVoice(null)
  }, [rvcVoices, rvcVoice])

  // 音色清单里没有当前选中项时（首次加载 / 音色被删），自动挑一个
  useEffect(() => {
    if (!voices.length) return
    if (selectedVoice && voices.some((v) => v.id === selectedVoice)) return
    setSelectedVoice(voices[0]?.id ?? null)
  }, [voices, selectedVoice])

  const start = useCallback(async () => {
    setStarting(true)
    setFeedback(null)
    try {
      const r = await cascadeStart({
        voiceId: selectedVoice ?? undefined,
        chunkMaxS: chunkMaxS,
        mode,
        rvcVoice: rvcVoice ?? undefined,
      })
      if (r.warming) {
        pendingRetryRef.current = true
        setFeedback({
          tone: "info",
          text: "TTS 模型加载中（首次约 30s），就绪后会自动开始，无需重复点击。",
        })
      } else {
        setFeedback({
          tone: "ok",
          text: r.hint ?? (r.already_running ? "级联变声已在运行中。" : "级联变声已启动。"),
        })
      }
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "启动失败") })
    } finally {
      setStarting(false)
    }
  }, [selectedVoice, chunkMaxS, mode, rvcVoice])

  const startRef = useRef(start)
  useEffect(() => {
    startRef.current = start
  }, [start])

  const running = Boolean(status?.running)

  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [s, vs] = await Promise.all([cascadeStatus(), listVoices()])
        if (!alive) return
        setStatus(s)
        setVoices(vs)
        // ASR 文字流：按 chunks 增量取 last_text（1s 轮询偶有漏句属正常，仅辅助核对）。
        // 未运行时残留的是上一会话状态，同步游标防止页面加载时回放旧文字
        if (s.running && s.chunks > lastChunksRef.current && s.last_text) {
          const text = s.last_text
          setTranscripts((prev) => [{ id: s.chunks, text }, ...prev].slice(0, 100))
        }
        lastChunksRef.current = Math.max(lastChunksRef.current, s.chunks)
        if (pendingRetryRef.current && s.worker_ready && !s.running) {
          pendingRetryRef.current = false
          void startRef.current()
        }
      } catch {
        /* 后端未启动时静默，页面顶部有服务状态指示 */
      }
    }
    void tick()
    const timer = window.setInterval(() => void tick(), running ? 1000 : 4000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [running])

  const stop = useCallback(async () => {
    setStopping(true)
    setFeedback(null)
    try {
      const r = await cascadeStop()
      if (!r.ok) {
        setFeedback({
          tone: "error",
          text: `停止失败：${r.error ?? "未知错误"}。可到左侧栏点「一键恢复音频」。`,
        })
        return
      }
      setFeedback({
        tone: "ok",
        text: r.note
          ? `${r.note}。已打开的微信/游戏需退出重开才会用回原麦克风。`
          : "已停止级联变声并还原声卡。已打开的微信/游戏需退出重开才会用回原麦克风。",
      })
    } catch (error) {
      setFeedback({ tone: "error", text: msgOf(error, "停止失败") })
    } finally {
      setStopping(false)
    }
  }, [])

  return {
    voices,
    selectedVoice,
    selectVoice: setSelectedVoice,
    rvcVoices,
    rvcVoice,
    setRvcVoice,
    status,
    running,
    mode,
    setMode,
    chunkMaxS,
    setChunkMaxS,
    starting,
    stopping,
    feedback,
    transcripts,
    start,
    stop,
  }
}
