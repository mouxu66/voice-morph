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
  const playingSrc = useAppStore((s) => s.playingSrc)
  const setPlayingSrc = useAppStore((s) => s.setPlayingSrc)

  // 桌面端 file:// 页面直接跨源加载 http 音频存在兼容性问题，改为先 fetch 取回
  // Blob 生成同源 objectURL 再交给 <audio>（服务端 CORS=*，fetch 必然成功）。
  useEffect(() => {
    const audio = audioRef.current
    if (!audio) return
    const prev = (audio as HTMLAudioElement & { __blobUrl?: string }).__blobUrl
    if (prev) {
      URL.revokeObjectURL(prev)
      ;(audio as HTMLAudioElement & { __blobUrl?: string }).__blobUrl = undefined
    }
    if (!src) {
      audio.src = ""
      setLoadError(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const res = await fetch(src)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        if (cancelled) return
        const blob = await res.blob()
        if (cancelled) return
        const objUrl = URL.createObjectURL(blob)
        ;(audio as HTMLAudioElement & { __blobUrl?: string }).__blobUrl = objUrl
        audio.src = objUrl
        audio.load()
        setLoadError(null)
      } catch (e) {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : "无法加载音频")
      }
    })()
    return () => {
      cancelled = true
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
    audio.addEventListener("timeupdate", update)
    audio.addEventListener("ended", stop)
    audio.addEventListener("pause", stop)
    if (audio.error) setLoadError(audio.error.message || "音频解码失败")
    return () => {
      audio.removeEventListener("timeupdate", update)
      audio.removeEventListener("ended", stop)
      audio.removeEventListener("pause", stop)
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

  const toggle = async () => {
    const audio = audioRef.current
    if (!audio) return
    if (audio.paused) {
      setPlayingSrc(src)
      try {
        await audio.play()
        setPlaying(true)
        setLoadError(null)
      } catch (e) {
        // 暴露真实失败原因，避免误以为没反应
        setLoadError(e instanceof Error ? e.message : "播放失败")
      }
    } else {
      audio.pause()
    }
  }

  return (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <audio ref={audioRef} preload="metadata" />
      <button
        type="button"
        onClick={toggle}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        aria-label={playing ? "暂停" : label}
      >
        {playing ? <Pause className="h-4 w-4" fill="currentColor" /> : <Play className="h-4 w-4" fill="currentColor" />}
      </button>
      <div className="min-w-0 flex-1">
        <StudioWaveform active={playing} className="h-8" />
        <div className="h-0.5 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary transition-[width] duration-200" style={{ width: `${progress * 100}%` }} />
        </div>
        {loadError ? (
          <div className="mt-0.5 truncate text-[10px] text-destructive">音频加载失败：{loadError}</div>
        ) : null}
      </div>
    </div>
  )
}
