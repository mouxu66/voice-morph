import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  marketBackups,
  marketCancel,
  marketInstall,
  marketInstalled,
  marketManifest,
  marketPreviewStatus,
  marketPreviewTrigger,
  marketProgress,
  marketRepo,
  marketRollback,
  marketSearch,
  marketUninstall,
  type MarketFile,
  type MarketFileSlot,
  type MarketItem,
  type MarketPreview,
  type MarketTask,
} from "@/api/client"

/** 安装进行中的状态集合（其余为终结态） */
const ACTIVE = new Set(["queued", "downloading_pth", "downloading_index", "staging"])

export type MarketPlatformFilter = "all" | "hf" | "modelscope"

/** 前端排队安装项：后端单安装互斥，任务进行中再点 → 先入队，当前任务完成后自动开始 */
export interface QueuedInstall {
  voice_id: string
  download: MarketFileSlot
  index?: MarketFileSlot | null
  display_name?: string
  manifest_id?: string
  overwrite?: boolean
}

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

  const refreshBackups = useCallback(async () => {
    try {
      setBackups(await marketBackups())
    } catch {
      /* 后端离线时忽略 */
    }
  }, [])

  useEffect(() => {
    void marketManifest()
      .then(setManifest)
      .catch(() => setManifest([]))
    void refreshInstalled()
    void refreshBackups()
  }, [refreshInstalled, refreshBackups])

  // 轮询全局进度：常驻 1.5s，busy 防慢响应叠加以防请求堆积
  // advanceQueue / ensurePreview 由后续声明，这里经 ref 异步调用（避免 TDZ 白屏）
  const advanceRef = useRef<() => void>(() => {})
  const ensurePreviewRef = useRef<(id: string, force?: boolean) => void>(() => {})
  useEffect(() => {
    let alive = true
    let busy = false
    const tick = async () => {
      if (busy) return                        // 慢响应不叠加请求
      busy = true
      try {
        const { task: t } = await marketProgress()
        if (!alive) return
        setTask(t)
        const st = t?.install?.status
        if (t && (st ?? t.status)) {
          setInstallingId(st ? t.install?.voice_id ?? "" : "")
        }
        // 状态由进行中 → 终结时：刷新已装列表、自动拉起试听生成、并续跑等待队列
        const cur = (st ?? t?.status) ?? ""
        const wasActive = ACTIVE.has(prevStatus.current)
        const finishedId = t?.install?.voice_id ?? ""
        if (cur && !ACTIVE.has(cur) && (wasActive || activeRef.current)) {
          activeRef.current = false
          // 后端异步失败（下载/落位失败）此前无任何提示，这里落进托盘错误条
          if (cur === "failed" && t?.install?.error) setInstallErr(t.install.error)
          void refreshInstalled()
          void refreshBackups()
          if (finishedId) void ensurePreviewRef.current(finishedId)
          advanceRef.current()
        }
        prevStatus.current = cur
      } catch {
        /* 轮询失败静默 */
      } finally {
        busy = false
      }
    }
    void tick()
    const timer = window.setInterval(tick, 1500)
    return () => {
      alive = false
      busy = false
      window.clearInterval(timer)
    }
  }, [refreshInstalled, refreshBackups])

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

  // ---- 安装编排（后端单安装互斥 + 前端排队：任务进行中再点 → 入队，完成后自动开始下一个）----
  const installRunning = !!task && !!task.install && ACTIVE.has(task.install.status)
  const [installQueue, setInstallQueue] = useState<QueuedInstall[]>([])
  const installQueueRef = useRef<QueuedInstall[]>([])
  const activeRef = useRef(false)         // 本地"安装请求是否在飞"（轮询有 1.5s 延迟，防连点误判）
  const queuedIds = useMemo(() => installQueue.map((q) => q.voice_id), [installQueue])

  const enqueue = useCallback((item: QueuedInstall) => {
    installQueueRef.current = [...installQueueRef.current, item]
    setInstallQueue(installQueueRef.current)
  }, [])
  const dequeue = useCallback((voice_id: string) => {
    installQueueRef.current = installQueueRef.current.filter((x) => x.voice_id !== voice_id)
    setInstallQueue(installQueueRef.current)
  }, [])
  const takeNext = useCallback((): QueuedInstall | null => {
    const [next, ...rest] = installQueueRef.current
    if (!next) return null
    installQueueRef.current = rest
    setInstallQueue(rest)
    return next
  }, [])

  const fireInstall = useCallback(async (q: QueuedInstall) => {
    activeRef.current = true
    setInstallErr("")
    setInstallingId(q.voice_id)
    try {
      const { task: t } = await marketInstall({
        voice_id: q.voice_id,
        download: q.download,
        index: q.index ?? undefined,
        display_name: q.display_name ?? q.voice_id,
        manifest_id: q.manifest_id ?? "",
        overwrite: q.overwrite ?? false,
      })
      setTask(t)
    } catch (e) {
      activeRef.current = false
      setInstallErr(e instanceof Error ? e.message : "安装发起失败")
      setInstallingId("")
      advanceRef.current()                 // 本次发起失败不阻塞队列：自动续下一个
    }
  }, [])

  const advanceQueue = useCallback(() => {
    if (activeRef.current) return
    const next = takeNext()
    if (next) void fireInstall(next)
  }, [fireInstall, takeNext])

  useEffect(() => {
    advanceRef.current = advanceQueue
  }, [advanceQueue])

  /** 发起安装：有任务在跑 → 加入等待队列；否则立即开始。同名已在队列/安装中则忽略。 */
  const startInstall = useCallback(
    (voice_id: string, download: MarketFileSlot, opts?: { index?: MarketFileSlot | null; display_name?: string; manifest_id?: string; overwrite?: boolean }) => {
      setInstallErr("")
      const item: QueuedInstall = {
        voice_id, download,
        index: opts?.index ?? null,
        display_name: opts?.display_name,
        manifest_id: opts?.manifest_id,
        overwrite: opts?.overwrite,
      }
      if (installQueueRef.current.some((x) => x.voice_id === voice_id)) return
      if (activeRef.current || installRunning || installingId === voice_id) {
        enqueue(item)
        return
      }
      void fireInstall(item)
    },
    [fireInstall, installRunning, installingId, enqueue],
  )

  /** 取消：voice_id 是排队项 → 出队；缺省（或正指向当前任务）→ 取消后端当前安装。 */
  const cancelInstall = useCallback(
    async (voice_id?: string) => {
      if (voice_id && installingId !== voice_id) {
        if (installQueueRef.current.some((x) => x.voice_id === voice_id)) {
          dequeue(voice_id)
          return
        }
      }
      try {
        const { task: t } = await marketCancel()
        setTask(t)
      } catch (e) {
        setInstallErr(e instanceof Error ? e.message : "取消失败")
      }
    },
    [installingId, dequeue],
  )

  // ---- 卸载（仅市场来源） ----
  const [uninstallingId, setUninstallingId] = useState("")
  const uninstallVoice = useCallback(
    async (voice_id: string) => {
      setInstallErr("")
      setUninstallingId(voice_id)
      try {
        const r = await marketUninstall(voice_id)
        await refreshInstalled()
        await refreshBackups()
        return r
      } catch (e) {
        setInstallErr(e instanceof Error ? e.message : "卸载失败")
        throw e
      } finally {
        setUninstallingId("")
      }
    },
    [refreshInstalled, refreshBackups],
  )

  // ---- 历史备份与回滚（覆盖重装自动归档，失败自动回滚，可手动回滚） ----
  const [backups, setBackups] = useState<string[]>([])
  const [rollbackingId, setRollbackingId] = useState("")

  const rollbackVoice = useCallback(
    async (voice_id: string) => {
      setInstallErr("")
      setRollbackingId(voice_id)
      try {
        const r = await marketRollback(voice_id)
        await Promise.all([refreshInstalled(), refreshBackups()])
        return r
      } catch (e) {
        setInstallErr(e instanceof Error ? e.message : "回滚失败")
        throw e
      } finally {
        setRollbackingId("")
      }
    },
    [refreshInstalled, refreshBackups],
  )

  // ---- 试听生成（A2：装完自动生成固定句试听，供市场卡片与音色库共用） ----
  const [previews, setPreviews] = useState<Record<string, MarketPreview>>({})
  const previewLocks = useRef<Set<string>>(new Set())

  /** 确保该音色试听可用：缺失/生成中就触发任务并轮询到终结（ready/failed/skipped）。
   *  force=true 时忽略终结态强制重新生成（用于「重试」按钮）。 */
  const ensurePreview = useCallback(
    async (voice_id: string, force = false) => {
      if (!voice_id || previewLocks.current.has(voice_id)) return
      previewLocks.current.add(voice_id)
      const put = (r: MarketPreview) => setPreviews((v) => ({ ...v, [voice_id]: r }))
      try {
        let r = await marketPreviewStatus(voice_id)
        if (!force && (r.status === "ready" || r.status === "failed" || r.status === "skipped")) {
          put(r)
          return
        }
        try {
          r = await marketPreviewTrigger(voice_id)
        } catch (e) {
          put({ status: "failed", url: "", error: e instanceof Error ? e.message : "试听生成启动失败" })
          return
        }
        for (let i = 0; i < 90; i++) {              // 最长 ~3 分钟（TTS 首启可能较慢）
          await new Promise((res) => setTimeout(res, 2000))
          try {
            r = await marketPreviewStatus(voice_id)
          } catch {
            r = { status: "missing", url: "", error: "" }
          }
          if (r.status === "ready" || r.status === "failed" || r.status === "skipped") {
            put(r)
            return
          }
        }
        put({ status: "failed", url: "", error: "试听生成超时，请稍后重试" })
      } catch (e) {
        put({ status: "failed", url: "", error: e instanceof Error ? e.message : "试听状态查询失败" })
      } finally {
        previewLocks.current.delete(voice_id)
      }
    }, [],
  )

  useEffect(() => {
    ensurePreviewRef.current = ensurePreview
  }, [ensurePreview])

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
    installQueue,
    queuedIds,
    installErr,
    setInstallErr,
    refreshInstalled,
    startInstall,
    cancelInstall,
    uninstallingId,
    uninstallVoice,
    backups,
    refreshBackups,
    rollbackingId,
    rollbackVoice,
    previews,
    ensurePreview,
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