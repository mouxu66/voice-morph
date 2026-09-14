import { useCallback, useEffect, useRef, useState } from "react"
import { Activity, Cable, Download, Eye, EyeOff, FolderOpen, HardDrive, LayoutGrid, Moon, Monitor, Palette, RotateCcw, Scale, Settings2, Sparkles, Stethoscope, Sun } from "lucide-react"
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom"
import { getHealth, listVoices, rvcLiveReset } from "@/api/client"
import { StudioNav } from "@/components/voice-studio/StudioNav"
import { EnvHealth } from "@/components/EnvHealth"
import { ModelSetupPanel, SetupBanner } from "@/components/ModelSetupPanel"
import { SendChainCheck } from "@/components/SendChainCheck"
import { StoragePanel } from "@/components/StoragePanel"
import { LicensesDialog } from "@/components/LicensesDialog"
import { PetGuide } from "@/components/PetGuide"
import { UpdateDialog } from "@/components/UpdateDialog"
import { ToastViewport } from "@/lib/notify"
import { appVersion, getSetupStatus, hasSetup, hasUpdate as canCheckUpdate, onUpdateAvailable, petGuide, saveSetup, type PetGuidePayload, type UpdateCheck } from "@/lib/electron"
import { useAppStore } from "@/store/useAppStore"
import { ThemeMode, getStoredSimpleMode, getStoredTheme, setStoredSimpleMode, setStoredTheme } from "@/theme"
import { LiveRoute } from "@/pages/Live/index"
import { TtsRoute } from "@/pages/Tts/index"
import { VoicesRoute } from "@/pages/Voices/index"
import { WorkshopRoute } from "@/pages/Workshop/index"
import { OfflineVcRoute } from "@/pages/OfflineVc/index"
import { PetMarketRoute } from "@/pages/PetMarket/index"

const pageTitles: Record<string, string> = {
  "/workshop": "音色工坊",
  "/voices": "音色库",
  "/live": "实时变声",
  "/tts": "语音合成",
  "/offlinevc": "离线工坊",
  "/pet-market": "人偶市场",
}

/** 桌宠换装首启引导载荷：首次启动由桌宠开口介绍"可以换样子"，设置里也可手动重播 */
function onboardingGuide(): PetGuidePayload {
  return {
    page: "pet-onboarding",
    title: "换装小贴士",
    lines: ["我是你的桌面人偶", "想要换个样子吗？", "去侧边栏「人偶市场」挑一个"],
    action: "wave",
    motion: "float",
    duration: 9500,
  }
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

function AppChrome({
  petGuideEnabled,
  onTogglePetGuide,
  simpleMode,
  onToggleSimple,
}: {
  petGuideEnabled: boolean
  onTogglePetGuide: () => void
  simpleMode: boolean
  onToggleSimple: () => void
}) {
  const currentLocation = useLocation()
  const { health } = useAppStore()
  const serviceState = useServiceState()
  const [theme, setTheme] = useState<ThemeMode>(getStoredTheme())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [envOpen, setEnvOpen] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)
  const [chainOpen, setChainOpen] = useState(false)
  const [storageOpen, setStorageOpen] = useState(false)
  // 开源许可：内容来自随包分发的 web/public/licenses（见 tools/sync_license_payload.py）
  const [licensesOpen, setLicensesOpen] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [restoreMsg, setRestoreMsg] = useState("")
  const [updateOpen, setUpdateOpen] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState<UpdateCheck | null>(null)
  const [version, setVersion] = useState<string | null>(null)
  const pageTitle = pageTitles[currentLocation.pathname] ?? "音色工坊"

  // 当前版本号（设置里显示）；非桌面端为 null
  useEffect(() => {
    void (async () => setVersion(await appVersion()))()
  }, [])

  // 应用启动 12s 后主进程静默检查，有新版就自动弹更新页（无新版不打扰）
  useEffect(() => onUpdateAvailable((r) => {
    setAutoUpdate(r)
    setUpdateOpen(true)
  }), [])

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
          <span className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-lg bg-[#1e293b] shadow-md">
            <img src="./icon-192.png" alt="" className="h-9 w-9 object-contain" />
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
          <StudioNav compact simpleMode={simpleMode} />
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
        <div className="absolute inset-x-0 bottom-0 h-px hairline-gradient opacity-70" aria-hidden="true" />
        <div className="flex h-16 items-center justify-between px-5 sm:px-8">
          <div className="flex items-center gap-4">
            <Link to="/workshop" className="flex items-center gap-2 lg:hidden">
              <span className="flex h-8 w-8 items-center justify-center overflow-hidden rounded-md bg-[#1e293b]">
                <img src="./icon-192.png" alt="" className="h-7 w-7 object-contain" />
              </span>
              <span className="text-sm font-semibold">变声工坊</span>
            </Link>
            <span className="hidden h-5 w-px bg-border lg:block" />
            <div>
              <p className="hidden font-mono text-xs uppercase tracking-widest text-muted-foreground sm:block">LOCAL VOICE WORKBENCH</p>
              <h1 className="text-sm font-semibold text-foreground">{pageTitle}</h1>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setEnvOpen(true)}
              className="flex items-center gap-2 text-xs text-muted-foreground transition hover:text-foreground"
              title="环境体检 / 启动本地服务"
            >
              <span className={`h-2 w-2 rounded-full ${online ? "bg-primary animate-pulse" : serviceState === "starting" ? "bg-yellow-500 animate-pulse" : "bg-destructive"}`} />
              <span className="hidden sm:inline">{online ? "本地服务在线" : serviceState === "starting" ? "服务启动中…" : "本地服务离线"}</span>
            </button>
            <button type="button" onClick={() => setChainOpen(true)} className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card text-muted-foreground shadow-md transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" aria-label="发送链路自检" title="发送链路自检：变声能不能送进微信/QQ/游戏">
              <Cable className="h-4 w-4" />
            </button>
            <button type="button" onClick={() => setEnvOpen(true)} className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card text-muted-foreground shadow-md transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" aria-label="环境体检">
              <Stethoscope className="h-4 w-4" />
            </button>
            <button type="button" onClick={() => setSettingsOpen((open) => !open)} className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-card text-muted-foreground shadow-md transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary" aria-label="打开设置">
              <Settings2 className="h-4 w-4" />
            </button>
          </div>
        </div>
        {settingsOpen && <div className="absolute right-5 top-14 w-48 rounded-lg border border-border bg-card p-2 shadow-lg sm:right-8"><p className="px-2 py-1 text-xs text-muted-foreground">界面主题</p><div className="mt-1 grid grid-cols-3 gap-1">{([['dark', '暗色', Moon], ['light', '亮色', Sun], ['system', '系统', Monitor]] as [ThemeMode, string, typeof Moon][]).map(([mode, label, Icon]) => <button type="button" key={mode} onClick={() => { setStoredTheme(mode); setTheme(mode); setSettingsOpen(false) }} className={`flex flex-col items-center gap-1 rounded-md px-2 py-2 text-xs ${theme === mode ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted'}`}><Icon className="h-4 w-4" />{label}</button>)}</div><button type="button" onClick={() => { onToggleSimple(); setSettingsOpen(false) }} className="mt-2 flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><span className="flex items-center gap-2"><LayoutGrid className="h-3.5 w-3.5" />极简模式</span><span className={`h-2 w-2 rounded-full ${simpleMode ? "bg-primary" : "bg-muted-foreground/30"}`} /></button><div className="mt-2 border-t border-border pt-2 space-y-1"><p className="px-2 py-1 text-xs text-muted-foreground">桌宠</p><button type="button" onClick={() => { window.dispatchEvent(new CustomEvent("replay-pet-guide")); setSettingsOpen(false) }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><Sparkles className="h-3.5 w-3.5" />让桌宠再讲一遍本页</button><button type="button" onClick={() => { window.dispatchEvent(new CustomEvent("replay-pet-onboarding")); setSettingsOpen(false) }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><Palette className="h-3.5 w-3.5" />重播换装引导</button>            <button type="button" onClick={() => onTogglePetGuide()} className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><span className="flex items-center gap-2">{petGuideEnabled ? <Eye className="h-3.5 w-3.5" /> : <EyeOff className="h-3.5 w-3.5" />}{petGuideEnabled ? "切页时介绍页面" : "已关闭切页介绍"}</span><span className={`h-2 w-2 rounded-full ${petGuideEnabled ? "bg-primary" : "bg-muted-foreground/30"}`} /></button></div><div className="mt-2 space-y-1 border-t border-border pt-2"><p className="px-2 py-1 text-xs text-muted-foreground">维护</p><button type="button" onClick={() => { setModelOpen(true); setSettingsOpen(false) }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><FolderOpen className="h-3.5 w-3.5" />模型与引擎配置</button><button type="button" onClick={() => { setStorageOpen(true); setSettingsOpen(false) }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><HardDrive className="h-3.5 w-3.5" />存储占用与清理</button></div><div className="mt-2 space-y-1 border-t border-border pt-2"><p className="px-2 py-1 text-xs text-muted-foreground">关于</p><button type="button" onClick={() => { setLicensesOpen(true); setSettingsOpen(false) }} className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><Scale className="h-3.5 w-3.5" />开源许可</button></div>{canCheckUpdate && <div className="mt-2 space-y-1 border-t border-border pt-2"><p className="px-2 py-1 text-xs text-muted-foreground">应用</p><button type="button" onClick={() => { setAutoUpdate(null); setUpdateOpen(true); setSettingsOpen(false) }} className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-xs text-muted-foreground transition hover:bg-muted hover:text-foreground"><span className="flex items-center gap-2"><Download className="h-3.5 w-3.5" />检查更新</span>{version && <span className="font-mono text-[10px] opacity-70">v{version}</span>}</button></div>}</div>}
      </header>
      <div className="fixed inset-x-0 top-16 z-10 border-b border-border bg-card/80 py-1.5 backdrop-blur-xl lg:hidden"><StudioNav simpleMode={simpleMode} /></div>
      {/* 缺模型时的全局降级提示：不阻塞启动，只提示相关功能不可用 */}
      <SetupBanner onOpen={() => setModelOpen(true)} />
      <EnvHealth open={envOpen} onClose={() => setEnvOpen(false)} />
      <ModelSetupPanel open={modelOpen} onClose={() => setModelOpen(false)} />
      <SendChainCheck open={chainOpen} onClose={() => setChainOpen(false)} />
      <StoragePanel open={storageOpen} onClose={() => setStorageOpen(false)} />
      <LicensesDialog open={licensesOpen} onClose={() => setLicensesOpen(false)} />
      <UpdateDialog
        open={updateOpen}
        onClose={() => { setUpdateOpen(false); setAutoUpdate(null) }}
        initialCheck={autoUpdate}
      />
    </>
  )
}

export default function App() {
  const { setHealth, setVoices } = useAppStore()
  const location = useLocation()
  const [petGuideEnabled, setPetGuideEnabled] = useState(true)
  const [simpleMode, setSimpleMode] = useState(getStoredSimpleMode())

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

  // ---- 桌宠换装首启引导 ----
  const onboardingFired = useRef(false)
  const petGuideEnabledRef = useRef(petGuideEnabled)
  petGuideEnabledRef.current = petGuideEnabled

  const sayPetOnboarding = useCallback(() => {
    petGuide(onboardingGuide())
  }, [])

  // 首次启动（且还没看过引导、导览开着）约 10s 后让桌宠开口一次——避开首屏切页讲解，
  // 随后落盘 petGuideSeen 标记；StrictMode 双跑用 ref 防重
  useEffect(() => {
    if (!hasSetup || onboardingFired.current) return
    onboardingFired.current = true
    let alive = true
    void getSetupStatus().then((st) => {
      if (!alive) return
      if (!st || st.config.petGuideSeen) return
      window.setTimeout(() => {
        if (petGuideEnabledRef.current) sayPetOnboarding()
        void saveSetup({ petGuideSeen: true })
      }, 10_000)
    })
    return () => { alive = false }
  }, [sayPetOnboarding])

  // 设置下拉「重播换装引导」：再次让桌宠开口介绍
  useEffect(() => {
    const onReplay = () => sayPetOnboarding()
    window.addEventListener("replay-pet-onboarding" as any, onReplay)
    return () => window.removeEventListener("replay-pet-onboarding" as any, onReplay)
  }, [sayPetOnboarding])

  return (
    <div className="relative min-h-[100dvh] bg-gradient-to-br from-background via-background to-card text-foreground">
      {/* 氛围层：纯 CSS 绘制（光晕/工程网格），不拦截交互、不参与布局 */}
      <div className="pointer-events-none fixed inset-0 z-0 ambient-glow" aria-hidden="true" />
      <div className="pointer-events-none fixed inset-0 z-0 ambient-grid" aria-hidden="true" />
      <AppChrome
        petGuideEnabled={petGuideEnabled}
        onTogglePetGuide={() => setPetGuideEnabled((v) => !v)}
        simpleMode={simpleMode}
        onToggleSimple={() => setSimpleMode((v) => {
          const next = !v
          setStoredSimpleMode(next)
          return next
        })}
      />
      <main className="relative min-h-[100dvh] pb-[calc(env(safe-area-inset-bottom)+1rem)] pt-[7.25rem] lg:pb-0 lg:pl-64 lg:pt-16">
        <Routes>
          <Route path="/workshop" element={<WorkshopRoute />} />
          <Route path="/voices" element={<VoicesRoute />} />
          <Route path="/live" element={<LiveRoute />} />
          <Route path="/tts" element={<TtsRoute />} />
          <Route path="/offlinevc" element={<OfflineVcRoute />} />
          <Route path="/pet-market" element={<PetMarketRoute />} />
          {/* 旧路由重定向到合并页对应 tab */}
          <Route path="/discover" element={<Navigate to="/workshop?tab=discover" replace />} />
          <Route path="/ft" element={<Navigate to="/workshop?tab=ft" replace />} />
          <Route path="/market" element={<Navigate to="/voices?tab=market" replace />} />
          <Route path="/qwen" element={<Navigate to="/live?tab=qwen" replace />} />
          <Route path="/cascade" element={<Navigate to="/live?tab=qwen" replace />} />
          <Route path="/audiobook" element={<Navigate to="/tts?tab=book" replace />} />
          <Route path="/wechat" element={<Navigate to="/tts?tab=wechat" replace />} />
          <Route path="/effects" element={<Navigate to="/offlinevc?tab=fx" replace />} />
          <Route path="*" element={<Navigate to="/live" replace />} />
        </Routes>
      </main>
      {/* 页面内导览桌宠：随路由切换介绍当前页 */}
      <PetGuide page={location.pathname} enabled={petGuideEnabled} />
      {/* 全局错误通知：任何未捕获异常 / 用户操作失败都会在此弹窗（见 lib/notify.tsx） */}
      <ToastViewport />
    </div>
  )
}

