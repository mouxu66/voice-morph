import { Pause, Play } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { cn } from "@/lib/utils"
import { StudioWaveform } from "@/components/voice-studio/StudioWaveform"
import { useAppStore } from "@/store/useAppStore"

type StudioAudioPlayerProps = {
  src: string
  label?: string
  className?: string
}

export function StudioAudioPlayer({ src, label = "试听", className }: StudioAudioPlayerProps) {
  const audioRef = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [progress, setProgress] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const playingSrc = useAppStore((s) => s.playingSrc)
  const setPlayingSrc = useAppStore((s) => s.setPlayingSrc)
  // 已装载进 <audio> 的源；blob URL 记在元素上，换源/卸载时统一回收
  const loadedSrcRef = useRef<string | null>(null)
  const srcRef = useRef(src)
  srcRef.current = src

  // 换源/卸载时回收 blob URL 并复位状态。音频改为「首次点播放」才
  // fetch → Blob → objectURL（桌面端 file:// 跨源直连 http 受限，服务端 CORS=*），
  // 避免挂载即全量下载整段 wav。
  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    loadedSrcRef.current = null
    audio.pause()
    audio.removeAttribute("src")
    audio.load()
    setPlaying(false)
    setProgress(0)
    setLoadError(null)
    return () => {
      const el = audio as HTMLAudioElement & { __blobUrl?: string }
      if (el.__blobUrl) {
        URL.revokeObjectURL(el.__blobUrl)
        el.__blobUrl = undefined
      }
    }
  }, [src])

  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    const update = () => setProgress(audio.duration ? audio.currentTime / audio.duration : 0)
    const stop = () => {
      setPlaying(false)
      setPlayingSrc((current) => (current === src ? null : current))
    }
    // 解码失败等 <audio> 层错误也要浮出，不能只在点播放时兜底
    const onErr = () => setLoadError(audio.error?.message || "音频解码失败")
    audio.addEventListener("timeupdate", update)
    audio.addEventListener("ended", stop)
    audio.addEventListener("pause", stop)
    audio.addEventListener("error", onErr)
    return () => {
      audio.removeEventListener("timeupdate", update)
      audio.removeEventListener("ended", stop)
      audio.removeEventListener("pause", stop)
      audio.removeEventListener("error", onErr)
    }
  }, [src, setPlayingSrc])

  // 全局联动：别的音频开始播放时，自动暂停本条
  useEffect(() => {
    const audio = audioRef.current
    if (!audio || !playing) return
    if (playingSrc && playingSrc !== src) {
      audio.pause()
      setPlaying(false)
    }
  }, [playingSrc, src, playing])

  const ensureLoaded = async (): Promise<boolean> => {
    const audio = audioRef.current
    if (!audio || !src) return false
    if (loadedSrcRef.current === src) return true
    setLoading(true)
    try {
      const res = await fetch(src)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      // 竞态防护：等待期间源已切换或组件已卸载时，丢弃结果
      if (srcRef.current !== src || !audioRef.current) return false
      const el = audio as HTMLAudioElement & { __blobUrl?: string }
      if (el.__blobUrl) URL.revokeObjectURL(el.__blobUrl)
      const objUrl = URL.createObjectURL(blob)
      el.__blobUrl = objUrl
      audio.src = objUrl
      audio.load()
      loadedSrcRef.current = src
      return true
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "无法加载音频")
      return false
    } finally {
      setLoading(false)
    }
  }

  const toggle = async () => {
    const audio = audioRef.current
    if (!audio || loading) return
    if (audio.paused) {
      setPlayingSrc(src)
      const ok = await ensureLoaded()
      if (!ok) {
        setPlayingSrc((current) => (current === src ? null : current))
        return
      }
      try {
        await audio.play()
        setPlaying(true)
        setLoadError(null)
      } catch (e) {
        // 暴露真实失败原因，避免误以为没反应
        setLoadError(e instanceof Error ? e.message : "播放失败")
        setPlaying(false)
      }
    } else {
      audio.pause()
    }
  }

  return (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <audio ref={audioRef} />
      <button
        type="button"
        onClick={toggle}
        disabled={loading}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-60"
        aria-label={playing ? "暂停" : label}
      >
        {playing ? <Pause className="h-4 w-4" fill="currentColor" /> : <Play className="h-4 w-4" fill="currentColor" />}
      </button>
      <div className="min-w-0 flex-1">
        <StudioWaveform active={playing} className="h-8" />
        <div className="h-0.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-[width] duration-200" style={{ width: `${progress * 100}%` }} />
        </div>
        {loading ? (
          <div className="mt-0.5 truncate text-[10px] text-muted-foreground">音频加载中…</div>
        ) : loadError ? (
          <div className="mt-0.5 truncate text-[10px] text-destructive">音频加载失败：{loadError}</div>
        ) : null}
      </div>
    </div>
  )
}
