import { useCallback, useEffect, useRef, useState } from "react"
import { Navigate, Route, Routes, useLocation } from "react-router-dom"
import { getHealth, listVoices } from "@/api/client"
import { AppChrome } from "@/components/layout/AppChrome"
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
import { getStoredSimpleMode, setStoredSimpleMode } from "@/theme"
import { HomeRoute } from "@/pages/Home/index"
import { FirstLaunchGuide } from "@/components/FirstLaunchGuide"
import { AuditionRoute } from "@/pages/Audition/index"
import { LiveRoute } from "@/pages/Live/index"
import { TtsRoute } from "@/pages/Tts/index"
import { VoicesRoute } from "@/pages/Voices/index"
import { WorkshopRoute } from "@/pages/Workshop/index"
import { OfflineVcRoute } from "@/pages/OfflineVc/index"
import { PetMarketRoute } from "@/pages/PetMarket/index"

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

export default function App() {
  const { setHealth, setVoices } = useAppStore()
  const location = useLocation()
  const [petGuideEnabled, setPetGuideEnabled] = useState(true)
  const [simpleMode, setSimpleMode] = useState(getStoredSimpleMode())

  // 弹窗开关集中在这里：外壳只负责触发，具体面板由 App 统一挂载
  const [envOpen, setEnvOpen] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)
  const [chainOpen, setChainOpen] = useState(false)
  const [storageOpen, setStorageOpen] = useState(false)
  const [licensesOpen, setLicensesOpen] = useState(false)
  const [updateOpen, setUpdateOpen] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState<UpdateCheck | null>(null)
  const [version, setVersion] = useState<string | null>(null)

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

  // 当前版本号（设置里显示）；非桌面端为 null
  useEffect(() => {
    void (async () => setVersion(await appVersion()))()
  }, [])

  // 应用启动 12s 后主进程静默检查，有新版就自动弹更新页（无新版不打扰）
  useEffect(() => onUpdateAvailable((r) => {
    setAutoUpdate(r)
    setUpdateOpen(true)
  }), [])

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

  // 设置抽屉「重播换装引导」：再次让桌宠开口介绍
  useEffect(() => {
    const onReplay = () => sayPetOnboarding()
    window.addEventListener("replay-pet-onboarding" as any, onReplay)
    return () => window.removeEventListener("replay-pet-onboarding" as any, onReplay)
  }, [sayPetOnboarding])

  return (
    <div className="relative min-h-[100dvh] bg-background text-foreground">
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
        onOpenEnv={() => setEnvOpen(true)}
        onOpenChain={() => setChainOpen(true)}
        onOpenModel={() => setModelOpen(true)}
        onOpenStorage={() => setStorageOpen(true)}
        onOpenLicenses={() => setLicensesOpen(true)}
        onOpenUpdate={() => { setAutoUpdate(null); setUpdateOpen(true) }}
        version={version}
        canCheckUpdate={canCheckUpdate}
      />

      {/* 窄屏 56px 顶栏 + 44px 横向导航，桌面端让开侧栏 */}
      <main className="relative min-h-[100dvh] pt-[100px] lg:pl-[var(--sidebar-w)] lg:pt-14">
        <Routes>
          <Route path="/home" element={<HomeRoute />} />
          <Route path="/" element={<Navigate to="/home" replace />} />
          <Route path="/workshop" element={<WorkshopRoute />} />
          <Route path="/voices" element={<VoicesRoute />} />
          <Route path="/live" element={<LiveRoute />} />
          <Route path="/audition" element={<AuditionRoute />} />
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
          <Route path="*" element={<Navigate to="/home" replace />} />
        </Routes>
      </main>

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

      {/* 页面内导览桌宠：随路由切换介绍当前页 */}
      <PetGuide page={location.pathname} enabled={petGuideEnabled} />
      {/* 首次启动引导：只弹一次，选择三条入口之一或先逛逛；设置里可重播 */}
      <FirstLaunchGuide />
      {/* 全局错误通知：任何未捕获异常 / 用户操作失败都会在此弹窗（见 lib/notify.tsx） */}
      <ToastViewport />
    </div>
  )
}
