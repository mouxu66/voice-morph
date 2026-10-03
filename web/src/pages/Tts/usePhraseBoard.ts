import { useCallback, useEffect, useRef, useState } from "react"
import {
  mediaUrl,
  voicePhraseCreate,
  voicePhraseDelete,
  voicePhraseList,
  voicePhraseSpeak,
  voicePhraseUpdate,
  type VoicePhrase,
} from "@/api/client"
import { useAppStore } from "@/store/useAppStore"
import { friendlyError } from "@/lib/errors"

/**
 * 快捷短语板：课堂高频用语一键播出。
 *
 * 与特效声板（`useSoundboard`）的分工：声板播的是**音效素材**（走常驻播放器进
 * 虚拟声卡），这里播的是**当前音色的话**（后端合成/命中缓存 → 浏览器 `<audio>`
 * 播放 → 走系统默认输出）。录网课/直播时把它当作"替我说这句"的按钮排。
 *
 * 合成期间互斥用 **ref**（`busyRef`）：`useState` 是异步的，同一事件循环里连点
 * 两次读到的还是旧值 —— 双击就会发出两个合成请求（冷态要等几十秒的那种），
 * ref 是同步写，当场可见（口径与 `useSoundboard` 的 `busyRef` 一致）。
 */
export function usePhraseBoard() {
  const { backendUp, selectedVoiceId } = useAppStore()
  const [items, setItems] = useState<VoicePhrase[]>([])
  /** 正在合成/播放的短语 id（按钮转圈 + 高亮都看它） */
  const [activeId, setActiveId] = useState("")
  const [error, setError] = useState("")
  /** CRUD 互斥：这些是毫秒级请求，同帧连点挡一下即可。 */
  const busyRef = useRef(false)
  /**
   * 合成互斥（与 busyRef 分开）：冷态合成要等几十秒，这把锁只挡"同一条的重复
   * 点击"。若与 CRUD 共用，合成期间连"加一条短语"都会被冻住 —— 那是两件无关的事。
   */
  const speakRef = useRef(false)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await voicePhraseList()
      setItems(r.items ?? [])
    } catch {
      /* 服务离线时静默：面板给空态，下次操作再试 */
    }
  }, [])

  useEffect(() => {
    if (!backendUp) return
    void refresh()
  }, [backendUp, refresh])

  useEffect(
    () => () => {
      audioRef.current?.pause()
    },
    [],
  )

  const add = useCallback(
    async (text: string) => {
      const t = text.trim()
      if (!t || busyRef.current) return
      busyRef.current = true
      setError("")
      try {
        const r = await voicePhraseCreate(t)
        setItems((prev) => [...prev, r.item])
      } catch (e) {
        setError(friendlyError(e, "添加短语失败"))
      } finally {
        busyRef.current = false
      }
    },
    [],
  )

  const update = useCallback(async (id: string, text: string) => {
    const t = text.trim()
    if (!t || busyRef.current) return
    busyRef.current = true
    setError("")
    try {
      const r = await voicePhraseUpdate(id, t)
      setItems((prev) => prev.map((it) => (it.id === id ? r.item : it)))
    } catch (e) {
      setError(friendlyError(e, "修改短语失败"))
    } finally {
      busyRef.current = false
    }
  }, [])

  const remove = useCallback(async (id: string) => {
    if (busyRef.current) return
    busyRef.current = true
    setError("")
    try {
      await voicePhraseDelete(id)
      setItems((prev) => prev.filter((it) => it.id !== id))
    } catch (e) {
      setError(friendlyError(e, "删除短语失败"))
    } finally {
      busyRef.current = false
    }
  }, [])

  /**
   * 点一下念一条：先请求后端（合成或命中缓存）拿到 URL，再交给浏览器播。
   *
   * 播新的之前先停掉上一条 —— 老师连点两条时听到的不该是叠在一起的两句话。
   * 播放期间 `activeId` 保持在这条上（不靠时长估结束，`onended` 说完了才算完）。
   */
  const play = useCallback(
    async (id: string) => {
      if (!backendUp || speakRef.current) return
      speakRef.current = true
      setError("")
      setActiveId(id)
      try {
        const r = await voicePhraseSpeak(id, selectedVoiceId || undefined)
        audioRef.current?.pause()
        const el = new Audio(mediaUrl(r.url))
        audioRef.current = el
        el.onended = () => setActiveId((cur) => (cur === id ? "" : cur))
        el.onerror = () => setActiveId((cur) => (cur === id ? "" : cur))
        await el.play()
      } catch (e) {
        setError(friendlyError(e, "播放失败"))
        setActiveId("")
      } finally {
        // 合成请求已结束；「正在播」的观感由 activeId + onended 维持，不归它管
        speakRef.current = false
      }
    },
    [backendUp, selectedVoiceId],
  )

  /** 掐掉正在播的那条（播错了不用等它念完）。没有在播时是安全的 no-op。 */
  const stop = useCallback(() => {
    audioRef.current?.pause()
    setActiveId("")
  }, [])

  return { items, activeId, error, hasVoice: Boolean(selectedVoiceId), refresh, add, update, remove, play, stop }
}
