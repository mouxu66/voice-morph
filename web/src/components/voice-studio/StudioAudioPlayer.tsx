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
  const playingSrc = useAppStore((s) => s.playingSrc)
  const setPlayingSrc = useAppStore((s) => s.setPlayingSrc)

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
      } catch {
        /* 播放失败（如文件不存在）静默处理 */
      }
    } else {
      audio.pause()
    }
  }

  return (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <audio ref={audioRef} src={src} preload="metadata" />
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
      </div>
    </div>
  )
}
