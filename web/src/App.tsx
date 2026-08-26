import { useCallback, useEffect, useState } from "react"
import { Activity, AudioWaveform, Moon, Monitor, RotateCcw, Settings2, Sun } from "lucide-react"
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom"
import { getHealth, listVoices, rvcLiveReset } from "@/api/client"
import { StudioNav } from "@/components/voice-studio/StudioNav"
import { useAppStore } from "@/store/useAppStore"
import { ThemeMode, getStoredTheme, setStoredTheme } from "@/theme"
import { KangarooRoute } from "@/pages/Kangaroo/index"
import { TtsRoute } from "@/pages/Tts/index"
import { VoicesRoute } from "@/pages/Voices/index"
import { WorkshopRoute } from "@/pages/Workshop/index"

const pageTitles: Record<string, string> = {
  "/kangaroo": "袋鼠语音",
  "/workshop": "音色工坊",
  "/voices": "音色库",
  "/tts": "文字转语音",
}

// 服务状态：在线 / 启动中（启动后 45s 内从未连上，视为正在加载模型）/ 离线
function useServiceState() {
  const backendUp = useAppStore((s) => s.backendUp)
  const lastOnlineAt = useAppStore((s) => s.lastOnlineAt)
  const appStartedAt = useAppStore((s) => s.appStartedAt)
  if (backendUp) return "online"
  if (lastOnlineAt == null && Date.now() - appStartedAt < 45_000) return "starting"
  return "offline"
}

function AppChrome() {
  const currentLocation = useLocation()
  const { health } = useAppStore()
  const serviceState = useServiceState()
  const [theme, setTheme] = useState<ThemeMode>(getStoredTheme())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [restoreMsg, setRestoreMsg] = useState("")
  const pageTitle = pageTitles[currentLocation.pathname] ?? "音色工坊"

  const handleRestoreAudio = useCallback(async () => {
    setRestoring(true)
    setRestoreMsg("")
    try {
      const r = await rvcLiveReset()
      if (!r.ok) {
        setRestoreMsg(`恢复失败：${r.error ?? "未知错误"}。可到 Windows 声音设置手动选择默认设备，或重启电脑。`)
        return
      }
      setRestoreMsg("已恢复默认音频设备（已打开的微信/游戏需退出重开才生效）")
    } catch (error) {
      setRestoreMsg(`恢复失败：${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setRestoring(false)
      window.setTimeout(() => setRestoreMsg(""), 5000)
    }
  }, [])
  const online = serviceState === "online"

  return (
    <>
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-border bg-card/75 px-4 py-5 shadow-lg backdrop-blur-xl lg:flex">
        <Link to="/workshop" className="flex items-center gap-3 px-2">
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-md">
            <AudioWaveform className="h-5 w-5" />
          </span>
          <span>
            <span className="block font-display text-sm font-semibold">变声工坊</span>
            <span className="mt-1 block font-mono text-xs text-muted-foreground">VOICE MORPH STUDIO</span>
          </span>
        </Link>
        <div className="mt-10 rounded-lg border border-border bg-background/70 p-4 shadow-md">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">工作区</p>
          <p className="mt-3 text-sm font-medium text-card-foreground">本地录音棚</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">质检、建库、转换，一处完成。</p>
        </div>
        <div className="mt-6">
          <button
            type="button"
            onClick={handleRestoreAudio}
            disabled={restoring}
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-primary/40 bg-primary/10 px-3 py-2.5 text-sm font-medium text-primary shadow-sm transition hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-60"
          >
            <RotateCcw className={restoring ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            {restoring ? "正在恢复…" : "一键恢复音频"}
          </button>
          {restoreMsg && <p className="mt-2 px-1 text-center text-xs leading-5 text-primary">{restoreMsg}</p>}
        </div>
        <div className="mt-8">
          <StudioNav compact />
        </div>
        <div className="mt-auto">
          <div className={`flex items-center gap-2 rounded-md border px-3 py-2.5 text-xs ${online ? "border-primary/30 bg-primary/10 text-primary" : serviceState === "starting" ? "border-yellow-500/40 bg-yellow-500/10 text-yellow-500" : "border-destructive/30 bg-destructive/10 text-destructive"}`}>
            <span className={`h-2 w-2 rounded-full ${online ? "bg-primary animate-pulse" : serviceState === "starting" ? "bg-yellow-500 animate-pulse" : "bg-destructive"}`} />
            <Activity className="h-3.5 w-3.5" />
            {online ? `服务在线${health?.cuda ? " · CUDA" : ""}` : serviceState === "starting" ? "服务启动中…" : "服务离线"}
          </div>
          <p className="mt-3 px-1 text-xs leading-5 text-muted-foreground">素材和结果保留在本地工作区。</p>
        </div>
      </aside>
      <header className="fixed inset-x-0 top-0 z-20 border-b border-border bg-background/85 backdrop-blur-xl lg:left-64">
        <div className="flex h-16 items-center justify-between px-5 sm:px-8">
          <div className="flex items-center gap-4">
            <Link to="/workshop" className="flex items-center gap-2 lg:hidden">
              <span className="flex h-8 w-8 items-center justify-center rounded-md bg-primary text-primary-foreground"><AudioWaveform className="h-4 w-4" /></span>
              <span className="text-sm font-semibold">变声工坊</span>
            </Link>
            <span className="hidden h-5 w-px bg-border lg:block" />
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-muted-foreground">LOCAL VOICE WORKBENCH</p>
              <h1 className="text-sm font-semibold text-foreground">{pageTitle}</h1>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className={`h-2 w-2 rounded-full ${online ? "bg-primary animate-pulse" : serviceState === "starting" ? "bg-yellow-500 animate-pulse" : "bg-destructive"}`} />
              {online ? "本地服务在线" : serviceState === "starting" ? "服务启动中…" : "本地服务离线"}
            </div>
            <button type="button" onClick={() => setSettingsOpen((open) => !open)} className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card text-muted-foreground shadow-md transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" aria-label="打开设置">
              <Settings2 className="h-4 w-4" />
            </button>
          </div>
        </div>
        {settingsOpen && <div className="absolute right-5 top-14 w-44 rounded-lg border border-border bg-card p-2 shadow-lg sm:right-8"><p className="px-2 py-1 text-xs text-muted-foreground">界面主题</p><div className="mt-1 grid grid-cols-3 gap-1">{([['dark', '暗色', Moon], ['light', '亮色', Sun], ['system', '系统', Monitor]] as [ThemeMode, string, typeof Moon][]).map(([mode, label, Icon]) => <button type="button" key={mode} onClick={() => { setStoredTheme(mode); setTheme(mode); setSettingsOpen(false) }} className={`flex flex-col items-center gap-1 rounded-md px-2 py-2 text-xs ${theme === mode ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted'}`}><Icon className="h-4 w-4" />{label}</button>)}</div></div>}
      </header>
      <div className="fixed inset-x-0 top-16 z-10 border-b border-border bg-card/80 px-5 py-2 backdrop-blur-xl lg:hidden"><StudioNav /></div>
    </>
  )
}

export default function App() {
  const { setHealth, setVoices } = useAppStore()

  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const [healthInfo, voiceItems] = await Promise.all([getHealth(), listVoices()])
        if (!alive) return
        setHealth(healthInfo)
        setVoices(voiceItems)
      } catch {
        if (alive) setHealth(null)
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 5000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [setHealth, setVoices])

  return <div className="min-h-screen bg-gradient-to-br from-background via-background to-card text-foreground"><AppChrome /><main className="min-h-screen pt-16 lg:pl-64"><Routes><Route path="/kangaroo" element={<KangarooRoute />} /><Route path="/workshop" element={<WorkshopRoute />} /><Route path="/voices" element={<VoicesRoute />} /><Route path="/tts" element={<TtsRoute />} /><Route path="*" element={<Navigate to="/kangaroo" replace />} /></Routes></main></div>
}
