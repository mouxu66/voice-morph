import { useCallback, useEffect, useRef, useState } from "react"
import {
  marketCancel,
  marketInstall,
  marketInstalled,
  marketManifest,
  marketProgress,
  marketRepo,
  marketSearch,
  type MarketFile,
  type MarketFileSlot,
  type MarketItem,
  type MarketTask,
} from "@/api/client"

/** 安装进行中的状态集合（其余为终结态） */
const ACTIVE = new Set(["queued", "downloading_pth", "downloading_index", "staging"])

export type MarketPlatformFilter = "all" | "hf" | "modelscope"

const AUDIO_RE = /\.(wav|mp3|flac|m4a|ogg)$/i

/** 由文件名推导安全 voice_id（RVC 目录名只允许字母数字_-），空则退化为仓库哈希 */
export function deriveVoiceId(name: string, repo: string): string {
  let s = (name || "").replace(/\.(pth|zip|index)$/i, "")
  s = s.replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48)
  if (s) return s
  let h = 5381
  for (let i = 0; i < repo.length; i++) h = (h * 33 + repo.charCodeAt(i)) >>> 0
  return `hf_${(h % 100000000).toString(36)}`
}

export function useVoiceMarket() {
  // ---- 基础：清单 / 已装 / 全局任务进度 ----
  const [manifest, setManifest] = useState<MarketItem[] | null>(null)
  const [installed, setInstalled] = useState<string[]>([])
  const [task, setTask] = useState<MarketTask | null>(null)
  const [installErr, setInstallErr] = useState("")
  const [installingId, setInstallingId] = useState("") // 正在安装的 voice_id（驱动卡片进度）
  const prevStatus = useRef<string>("")

  const refreshInstalled = useCallback(async () => {
    try {
      setInstalled(await marketInstalled())
    } catch {
      /* 后端离线时忽略 */
    }
  }, [])

  useEffect(() => {
    void marketManifest()
      .then(setManifest)
      .catch(() => setManifest([]))
    void refreshInstalled()
  }, [refreshInstalled])

  // 轮询全局进度：常驻 1.5s（兼恢复上一次会话遗留的安装任务）
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const { task: t } = await marketProgress()
        if (!alive) return
        setTask(t)
        const st = t?.install?.status
        if (t && (st ?? t.status)) {
          setInstallingId(st ? t.install?.voice_id ?? "" : "")
        }
        // 状态由进行中 → 终结时，刷新已装列表
        const cur = (st ?? t?.status) ?? ""
        const wasActive = ACTIVE.has(prevStatus.current)
        if (wasActive && !ACTIVE.has(cur)) void refreshInstalled()
        prevStatus.current = cur
      } catch {
        /* 轮询失败静默 */
      }
    }
    void tick()
    const timer = window.setInterval(tick, 1500)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [refreshInstalled])

  // ---- 搜索 ----
  const [searching, setSearching] = useState(false)
  const [searchQuery, setSearchQuery] = useState("")
  const [platform, setPlatform] = useState<MarketPlatformFilter>("all")
  const [results, setResults] = useState<MarketItem[] | null>(null)
  const [searchNote, setSearchNote] = useState<string | null>(null)

  const doSearch = useCallback(async (q: string, pf: MarketPlatformFilter) => {
    const query = q.trim()
    if (!query) return
    setSearching(true)
    setSearchNote(null)
    setRepoOpen(null)
    try {
      const r = await marketSearch(query, pf, 50)
      setResults(r.items)
      setSearchNote(r.note)
    } catch (e) {
      setResults([])
      setSearchNote(e instanceof Error ? e.message : "搜索失败")
    } finally {
      setSearching(false)
    }
  }, [])

  // ---- 仓库文件面板（搜索结果的安装选文件） ----
  const [repoOpen, setRepoOpen] = useState<MarketItem | null>(null)
  const [repoFiles, setRepoFiles] = useState<MarketFile[] | null>(null)
  const [repoReadme, setRepoReadme] = useState<string | null>(null)
  const [repoLoading, setRepoLoading] = useState(false)
  const [repoErr, setRepoErr] = useState("")
  const [pickPth, setPickPth] = useState<MarketFile | null>(null)
  const [pickIdx, setPickIdx] = useState<MarketFile | null>(null)

  const openRepo = useCallback(async (item: MarketItem) => {
    if (repoOpen?.id === item.id) {
      setRepoOpen(null)
      return
    }
    setRepoOpen(item)
    setRepoFiles(null)
    setRepoReadme(null)
    setPickPth(null)
    setPickIdx(null)
    setRepoErr("")
    setRepoLoading(true)
    try {
      const r = await marketRepo(item.repo, item.platform, false)
      setRepoFiles(r.files)
      setRepoReadme(r.readme ?? null)
    } catch (e) {
      setRepoErr(e instanceof Error ? e.message : "仓库文件加载失败")
    } finally {
      setRepoLoading(false)
    }
  }, [repoOpen])

  // ---- 安装编排 ----
  const installRunning = !!task && !!task.install && ACTIVE.has(task.install.status)

  const startInstall = useCallback(
    async (voice_id: string, download: MarketFileSlot, opts?: { index?: MarketFileSlot | null; display_name?: string; manifest_id?: string }) => {
      setInstallErr("")
      setInstallingId(voice_id)
      try {
        const { task: t } = await marketInstall({
          voice_id,
          download,
          index: opts?.index ?? undefined,
          display_name: opts?.display_name ?? voice_id,
          manifest_id: opts?.manifest_id ?? "",
        })
        setTask(t)
      } catch (e) {
        setInstallErr(e instanceof Error ? e.message : "安装发起失败")
        setInstallingId("")
      }
    },
    [],
  )

  const cancelInstall = useCallback(async () => {
    try {
      const { task: t } = await marketCancel()
      setTask(t)
    } catch (e) {
      setInstallErr(e instanceof Error ? e.message : "取消失败")
    }
  }, [])

  // ---- 派生帮助 ----
  /** 搜索结果的快速安装槽：files 恰好一个 pth → 可一键；否则需打开文件面板选 */
  const quickSlot = useCallback((item: MarketItem): { download: MarketFile; index: MarketFile | null } | null => {
    if (item.prefs?.download) return null // 清单预置条目走 startInstall 直链分支
    const pths = (item.files ?? []).filter((f) => f.type === "model" && /\.pth$/i.test(f.path))
    if (pths.length !== 1) return null
    const indexes = (item.files ?? []).filter((f) => /\.index$/i.test(f.path))
    return { download: pths[0], index: indexes[0] ?? null }
  }, [])

  /** 资源是否可试听（真音频文件才能播；.pth 权重不算） */
  const isPlayable = useCallback((url?: string) => !!url && AUDIO_RE.test(url) && !/\.pth$/i.test(url), [])

  /** 文件面板里的演示音频（优先 demo/试听/sample 命名的） */
  const demoAudio = useCallback(
    (files: MarketFile[]): MarketFile | null => {
      const audios = files.filter((f) => f.type !== "directory" && AUDIO_RE.test(f.path))
      if (!audios.length) return null
      return (
        audios.find((f) => /(demo|试听|sample|preview|示意)/i.test(f.path)) ?? audios[0]
      )
    },
    [],
  )

  return {
    manifest,
    installed,
    task,
    installRunning,
    installingId,
    installErr,
    setInstallErr,
    refreshInstalled,
    startInstall,
    cancelInstall,
    searching,
    searchQuery,
    setSearchQuery,
    platform,
    setPlatform,
    results,
    searchNote,
    doSearch,
    repoOpen,
    openRepo,
    repoFiles,
    repoReadme,
    repoLoading,
    repoErr,
    pickPth,
    setPickPth,
    pickIdx,
    setPickIdx,
    quickSlot,
    isPlayable,
    demoAudio,
    deriveVoiceId,
  }
}

export type VoiceMarket = ReturnType<typeof useVoiceMarket>