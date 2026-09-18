import { useCallback, useEffect, useState } from "react"
import { Cable, Menu, RotateCcw, Settings2, X } from "lucide-react"
import { Link, useLocation } from "react-router-dom"
import { rvcLiveReset } from "@/api/client"
import { StudioNav } from "@/components/voice-studio/StudioNav"
import { SettingsPanel } from "@/components/layout/SettingsPanel"
import { cn } from "@/lib/utils"
import { useAppStore } from "@/store/useAppStore"
import { getStoredTheme, setStoredTheme, type ThemeMode } from "@/theme"

const pageTitles: Record<string, string> = {
  "/home": "首页",
  "/workshop": "训练变声",
  "/voices": "我的音色",
  "/live": "实时变声",
  "/tts": "输字变声",
  "/offlinevc": "工具箱",
  "/pet-market": "桌宠皮肤",
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

/**
 * 应用外壳：左侧主导航 + 顶部工具栏。
 *
 * 相对上一版的取舍：
 * - 侧栏去掉了「工作区 / 本地录音棚」那张零信息量的装饰卡（它占了首屏最贵的位置，
 *   却只说了一句"质检、建库、转换，一处完成"），导航整体上移；
 * - 服务状态此前在侧栏底部和顶栏各出现一次，现在只保留顶栏一处（它同时是环境体检入口）；
 * - 顶栏原来并列四个入口，其中「状态点」与「环境体检」通往同一个弹窗，已合并为状态胶囊；
 * - 窄屏此前完全没有导航入口（侧栏 hidden 无替代），现在补了抽屉。
 *
 * 侧栏宽度由 index.css 的 --sidebar-w 单点定义，顶栏偏移与主内容左内边距都读它。
 */
export function AppChrome({
  petGuideEnabled,
  onTogglePetGuide,
  simpleMode,
  onToggleSimple,
  onOpenEnv,
  onOpenModel,
  onOpenStorage,
  onOpenLicenses,
  onOpenUpdate,
  onOpenChain,
  version,
  canCheckUpdate,
}: {
  petGuideEnabled: boolean
  onTogglePetGuide: () => void
  simpleMode: boolean
  onToggleSimple: () => void
  onOpenEnv: () => void
  onOpenModel: () => void
  onOpenStorage: () => void
  onOpenLicenses: () => void
  onOpenUpdate: () => void
  onOpenChain: () => void
  version: string | null
  canCheckUpdate: boolean
}) {
  const { pathname } = useLocation()
  const cuda = useAppStore((s) => s.health?.cuda)
  const serviceState = useServiceState()
  const [theme, setTheme] = useState<ThemeMode>(getStoredTheme())
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [restoreMsg, setRestoreMsg] = useState("")
  const pageTitle = pageTitles[pathname] ?? "首页"
  const online = serviceState === "online"

  // 换页收起窄屏抽屉，避免切页后抽屉还挂在屏幕上
  useEffect(() => {
    setDrawerOpen(false)
  }, [pathname])

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
      window.setTimeout(() => setRestoreMsg(""), 6000)
    }
  }, [])

  const sidebarBody = (
    <>
      <Link to="/home" className="flex items-center gap-2.5 px-5 py-4">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-[#1e293b] shadow-md">
          <img src="./icon-192.png" alt="" className="h-8 w-8 object-contain" />
        </span>
        <span className="min-w-0">
          <span className="block truncate text-sm font-semibold text-foreground">变声工坊</span>
          <span className="block truncate text-[11px] text-muted-foreground">本地音色工作台</span>
        </span>
      </Link>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-2">
        <StudioNav variant="sidebar" simpleMode={simpleMode} onNavigate={() => setDrawerOpen(false)} />
      </div>

      <div className="border-t border-border px-3 py-3">
        <button
          type="button"
          onClick={() => void handleRestoreAudio()}
          disabled={restoring}
          title="变声会把系统默认声卡切到虚拟声卡，这里一键切回你原来的设备"
          className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-xs text-muted-foreground transition hover:bg-muted/60 hover:text-foreground disabled:opacity-60"
        >
          <RotateCcw className={cn("h-3.5 w-3.5 shrink-0", restoring && "animate-spin")} />
          {restoring ? "正在恢复…" : "一键恢复音频"}
        </button>
        {restoreMsg && <p className="mt-1 px-3 text-[11px] leading-4 text-primary">{restoreMsg}</p>}
      </div>
    </>
  )

  return (
    <>
      {/* 桌面侧栏 */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-[var(--sidebar-w)] flex-col border-r border-border bg-card/70 backdrop-blur-xl lg:flex">
        {sidebarBody}
      </aside>

      {/* 窄屏抽屉 */}
      {drawerOpen && (
        <div className="fixed inset-0 z-40 bg-black/60 lg:hidden" onClick={() => setDrawerOpen(false)}>
          <aside
            className="relative flex h-full w-[272px] flex-col border-r border-border bg-card"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-label="导航"
          >
            <button
              type="button"
              onClick={() => setDrawerOpen(false)}
              aria-label="收起导航"
              className="absolute right-3 top-4 flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
            {sidebarBody}
          </aside>
        </div>
      )}

      {/* 顶栏 */}
      <header className="fixed left-0 right-0 top-0 z-20 border-b border-border bg-background/85 backdrop-blur-xl lg:left-[var(--sidebar-w)]">
        <div className="absolute inset-x-0 bottom-0 h-px hairline-gradient opacity-60" aria-hidden="true" />
        <div className="flex h-14 items-center justify-between gap-3 px-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              onClick={() => setDrawerOpen(true)}
              aria-label="打开导航"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-sm transition hover:border-primary hover:text-primary lg:hidden"
            >
              <Menu className="h-4 w-4" />
            </button>
            <Link to="/home" className="flex shrink-0 items-center gap-2 lg:hidden">
              <span className="flex h-7 w-7 items-center justify-center overflow-hidden rounded-md bg-[#1e293b]">
                <img src="./icon-192.png" alt="" className="h-6 w-6 object-contain" />
              </span>
            </Link>
            <h1 className="truncate text-sm font-semibold text-foreground">{pageTitle}</h1>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={onOpenEnv}
              title="本地服务状态 · 点击打开环境体检"
              className={cn(
                "flex h-9 items-center gap-2 rounded-full border px-3 text-xs shadow-sm transition",
                online
                  ? "border-primary/35 bg-primary/10 text-primary hover:bg-primary/15"
                  : serviceState === "starting"
                    ? "border-yellow-500/50 bg-yellow-500/15 text-yellow-600 hover:bg-yellow-500/20"
                    : "border-destructive/35 bg-destructive/10 text-destructive hover:bg-destructive/15",
              )}
            >
              <span
                className={cn(
                  "h-2 w-2 shrink-0 rounded-full",
                  online ? "animate-pulse bg-primary" : serviceState === "starting" ? "animate-pulse bg-yellow-500" : "bg-destructive",
                )}
              />
              <span className="hidden sm:inline">
                {online ? `服务在线${cuda ? " · CUDA" : ""}` : serviceState === "starting" ? "服务启动中" : "服务离线"}
              </span>
            </button>
            <button
              type="button"
              onClick={onOpenChain}
              className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-sm transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              aria-label="发送链路自检"
              title="发送链路自检：变声能不能送进微信/QQ/游戏"
            >
              <Cable className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={() => setSettingsOpen(true)}
              className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-muted-foreground shadow-sm transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
              aria-label="打开设置"
              title="设置"
            >
              <Settings2 className="h-4 w-4" />
            </button>
          </div>
        </div>
      </header>

      {/* 窄屏横向导航：贴在顶栏下沿，不再用魔法 padding 顶开 */}
      <div className="fixed inset-x-0 top-14 z-10 border-b border-border bg-card/85 backdrop-blur-xl lg:hidden">
        <StudioNav variant="bar" onNavigate={() => setDrawerOpen(false)} />
      </div>

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        theme={theme}
        onThemeChange={(mode) => {
          setStoredTheme(mode)
          setTheme(mode)
        }}
        simpleMode={simpleMode}
        onToggleSimple={onToggleSimple}
        petGuideEnabled={petGuideEnabled}
        onTogglePetGuide={onTogglePetGuide}
        version={version}
        canCheckUpdate={canCheckUpdate}
        onOpenModel={onOpenModel}
        onOpenStorage={onOpenStorage}
        onOpenLicenses={onOpenLicenses}
        onOpenUpdate={onOpenUpdate}
      />
    </>
  )
}
