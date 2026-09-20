import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Navigate, Route, Routes, useLocation } from "react-router-dom"
import { getHealth, listVoices } from "@/api/client"
import { AppChrome } from "@/components/layout/AppChrome"
import { EnvHealth } from "@/components/EnvHealth"
import { ModelSetupPanel, SetupBanner } from "@/components/ModelSetupPanel"
import { SendChainCheck } from "@/components/SendChainCheck"
import { StoragePanel } from "@/components/StoragePanel"
import { CapabilityPanel } from "@/components/CapabilityPanel"
import { LicensesDialog } from "@/components/LicensesDialog"
import { PetGuide } from "@/components/PetGuide"
import { UpdateDialog } from "@/components/UpdateDialog"
import { NoticePanel, RouteBoundary, RouteFallback } from "@/components/RouteBoundary"
import { ToastViewport } from "@/lib/notify"
import { buildRoutes, usePluginCatalog } from "@/lib/pluginRoutes"
import { appVersion, getSetupStatus, hasSetup, hasUpdate as canCheckUpdate, onUpdateAvailable, petGuide, saveSetup, type PetGuidePayload, type UpdateCheck } from "@/lib/electron"
import { useAppStore } from "@/store/useAppStore"
import { getStoredSimpleMode, setStoredSimpleMode } from "@/theme"
import { FirstLaunchGuide } from "@/components/FirstLaunchGuide"

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
  const [capOpen, setCapOpen] = useState(false)
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

  // ---- 页面路由由能力清单驱动（插件化第 4 步）----
  // 清单里声明了「哪些能力存在、每个能力的页面在哪个模块、导出叫什么名字」，
  // 这里只负责把它变成 <Route>。见 lib/pluginRoutes.tsx 的三条注意事项。
  const { state: catalogState, reload: reloadCatalog } = usePluginCatalog()

  const routePlan = useMemo(() => {
    if (catalogState.status !== "ready") return null
    const { routes, legacy } = buildRoutes(catalogState.catalog)
    // `/` 与 `*` 两条兜底要重定向到首页，所以首页必须存在。真找不到就退回第一条可用路由，
    // 一条都没有则交给下面渲染「没有可用页面」—— **绝不能让 `*` 指向一个不存在的路径**，
    // 那会变成无限重定向，症状是整页卡死而不是报错，极难往回查。
    const home = routes.some((r) => r.path === "/home") ? "/home" : (routes[0]?.path ?? null)
    return { routes, legacy, home }
  }, [catalogState])

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
        onOpenCapabilities={() => setCapOpen(true)}
        onOpenUpdate={() => { setAutoUpdate(null); setUpdateOpen(true) }}
        version={version}
        canCheckUpdate={canCheckUpdate}
      />

      {/* 窄屏 56px 顶栏 + 44px 横向导航，桌面端让开侧栏 */}
      <main className="relative min-h-[100dvh] pt-[100px] lg:pl-[var(--sidebar-w)] lg:pt-14">
        {/* 三态：清单读取失败 / 一条路由都没有 / 正常渲染。
            前两态**不渲染 <Routes>** —— 否则 `*` 兜底会把人往一个不存在的首页上引。 */}
        {routePlan === null ? (
          catalogState.status === "error" ? (
            <NoticePanel
              title="读不到能力清单"
              desc="界面上的页面清单来自后端的 /api/plugins。拿不到它就没法知道有哪些页面可去，所以这里不猜、也不降级成空界面。后端可能刚启动或已退出。"
              detail={catalogState.message}
              actionLabel="重试"
              onAction={reloadCatalog}
            />
          ) : (
            <RouteFallback />
          )
        ) : routePlan.home === null ? (
          <NoticePanel
            title="没有可用的页面"
            desc="能力清单里一条页面路由都没有。这通常意味着后端清单被改坏了（plugins/<id>/plugin.json 的 routes）。"
            actionLabel="重新加载"
            onAction={() => window.location.reload()}
          />
        ) : (
          <RouteBoundary>
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                {/* 真页面：路径 / 模块 / 导出全部来自清单，前端不再手写 */}
                {routePlan.routes.map(({ path, Component }) => (
                  <Route key={path} path={path} element={<Component />} />
                ))}
                {/* 旧路由重定向到合并页对应 tab（清单的 legacy_routes） */}
                {routePlan.legacy.map(({ path, redirect }) => (
                  <Route key={path} path={path} element={<Navigate to={redirect} replace />} />
                ))}
                {/* 外壳兜底：不属于任何插件 */}
                <Route path="/" element={<Navigate to={routePlan.home} replace />} />
                <Route path="*" element={<Navigate to={routePlan.home} replace />} />
              </Routes>
            </Suspense>
          </RouteBoundary>
        )}
      </main>

      {/* 缺模型时的全局降级提示：不阻塞启动，只提示相关功能不可用 */}
      {/* 「看诊断」直接开到环境体检：能力没挂上时那面板里的勾叉清单比配置向导更对口 */}
      <SetupBanner onOpen={() => setModelOpen(true)} onDiagnose={() => setEnvOpen(true)} />
      <EnvHealth open={envOpen} onClose={() => setEnvOpen(false)} />
      <ModelSetupPanel open={modelOpen} onClose={() => setModelOpen(false)} />
      <SendChainCheck open={chainOpen} onClose={() => setChainOpen(false)} />
      <StoragePanel open={storageOpen} onClose={() => setStorageOpen(false)} />
      <CapabilityPanel open={capOpen} onClose={() => setCapOpen(false)} />
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
