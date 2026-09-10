import { useCallback, useEffect, useRef, useState } from "react"
import {
  petApplied,
  petApply,
  petCancel,
  petDetail,
  petDiscovery,
  petDiscoveryRemove,
  petInstall,
  petInstalled,
  petManifest,
  petProgress,
  petScanCancel,
  petScanProgress,
  petScanStart,
  petUninstall,
  type PetAppliedSkin,
  type PetDiscoveryItem,
  type PetInstalledItem,
  type PetScanProgress,
  type PetSkinDetail,
  type PetSkinItem,
  type PetTaskItem,
} from "@/api/client"
import { notify } from "@/lib/notify"

/** 进行中的状态集合（排队/下载/转换都算活跃；其余为终结态） */
export const PET_ACTIVE = new Set(["downloading", "installing"])
export const PET_BUSY = new Set(["queued", "downloading", "installing"])

export type PetMarket = ReturnType<typeof usePetMarket>

export function usePetMarket() {
  const [manifest, setManifest] = useState<PetSkinItem[] | null>(null)
  const [installed, setInstalled] = useState<PetInstalledItem[]>([])
  const [applied, setApplied] = useState<PetAppliedSkin | null>(null)
  const [tasks, setTasks] = useState<PetTaskItem[]>([])
  const [installErr, setInstallErr] = useState("")
  // 详情面板
  const [detail, setDetail] = useState<PetSkinDetail | null>(null)
  const [detailId, setDetailId] = useState("")
  const [detailLoading, setDetailLoading] = useState(false)
  const busyRef = useRef(false)             // 轮询慢响应不叠加
  const prevStatus = useRef(new Map<string, string>())

  const refreshInstalled = useCallback(async () => {
    try {
      setInstalled(await petInstalled())
    } catch {
      /* 后端离线时忽略 */
    }
  }, [])

  const refreshApplied = useCallback(async () => {
    try {
      setApplied(await petApplied())
    } catch {
      /* 后端离线时忽略 */
    }
  }, [])

  useEffect(() => {
    void petManifest().then(setManifest).catch(() => setManifest([]))
    void refreshInstalled()
    void refreshApplied()
  }, [refreshInstalled, refreshApplied])

  // 轮询任务队列：1s 一次；任务从进行中 → 终结态时刷新已装列表并提示
  useEffect(() => {
    let alive = true
    const timer = window.setInterval(async () => {
      if (busyRef.current) return
      busyRef.current = true
      try {
        const r = await petProgress()
        if (!alive) return
        setTasks(r.items)
        const prev = prevStatus.current
        const now = new Map(r.items.map((t) => [t.skin_id, t.status]))
        let changed = false
        for (const t of r.items) {
          const was = prev.get(t.skin_id)
          if (was && PET_ACTIVE.has(was) && !PET_ACTIVE.has(t.status)) {
            changed = true
            if (t.status === "failed") {
              setInstallErr(t.error || `「${t.skin_id}」皮肤安装失败`)
            } else if (t.status === "done") {
              notify.success(`「${t.skin_id}」皮肤安装完成，可在皮肤库换肤应用`)
            } else if (t.status === "cancelled") {
              notify.info(`「${t.skin_id}」安装已取消`)
            }
          }
        }
        prevStatus.current = now
        if (changed) {
          void refreshInstalled()
          void refreshApplied()
        }
      } catch {
        /* 后端离线 */
      } finally {
        busyRef.current = false
      }
    }, 1000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [refreshInstalled, refreshApplied])

  // 安装 / 取消 / 应用 / 卸载
  const startInstall = useCallback(async (id: string) => {
    setInstallErr("")
    try {
      const t = await petInstall(id)
      if (t.status === "queued") notify.info(`「${id}」已加入安装队列`)
      setTasks((await petProgress()).items)
    } catch (e) {
      setInstallErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const cancelTask = useCallback(async (id: string) => {
    try {
      await petCancel(id)
      notify.info(`已请求取消「${id}」安装`)
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const applySkin = useCallback(
    async (id: string) => {
      try {
        await petApply(id)
        notify.success("皮肤已应用，桌面人偶会在几秒内换上新外观")
        void refreshApplied()
        void refreshInstalled()
      } catch (e) {
        notify.error(e instanceof Error ? e.message : String(e))
      }
    },
    [refreshApplied, refreshInstalled],
  )

  const uninstallSkin = useCallback(
    async (id: string) => {
      try {
        const r = await petUninstall(id)
        notify.success(r.reset_applied ? "已卸载并恢复默认皮肤" : "已卸载")
        void refreshInstalled()
        void refreshApplied()
      } catch (e) {
        notify.error(e instanceof Error ? e.message : String(e))
      }
    },
    [refreshInstalled, refreshApplied],
  )

  // 详情面板
  const openDetail = useCallback(async (id: string) => {
    setDetailId(id)
    setDetailLoading(true)
    try {
      setDetail(await petDetail(id))
    } catch {
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const closeDetail = useCallback(() => {
    setDetail(null)
    setDetailId("")
  }, [])

  // ---- GitHub 扫描器（发现 Tab） ----
  const [scan, setScan] = useState<PetScanProgress>(() => ({
    status: "idle", phase: "", step: "", total: 0, current: 0,
    repos_seen: 0, repos_lic_skip: 0, repos_tree_skip: 0,
    candidates: 0, built_ok: 0, built_fail: 0, atlas_skip: 0,
    error: "", finished_at: "",
  }))
  const [discovery, setDiscovery] = useState<PetDiscoveryItem[] | null>(null)
  const scanBusy = useRef(false)

  const refreshDiscovery = useCallback(async () => {
    try {
      setDiscovery(await petDiscovery())
    } catch {
      /* 后端离线时忽略 */
    }
  }, [])

  // 扫描进行中：1s 轮询进度；结束后刷新发现列表
  const scannerRunning = scan.status === "running"
  useEffect(() => {
    if (!scannerRunning) return
    let alive = true
    const timer = window.setInterval(async () => {
      if (scanBusy.current) return
      scanBusy.current = true
      try {
        const p = await petScanProgress()
        if (!alive) return
        setScan(p)
        if (p.status !== "running") void refreshDiscovery()
      } catch {
        /* 后端离线 */
      } finally {
        scanBusy.current = false
      }
    }, 1000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [scannerRunning, refreshDiscovery])

  const startScan = useCallback(async () => {
    try {
      setScan(await petScanStart())
      notify.info("开始扫描 GitHub 开源桌宠素材…")
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e))
    }
  }, [])

  // 挂载时同步一次扫描状态（页面刷新后仍能看到进行中的扫描）与发现列表
  useEffect(() => {
    void petScanProgress().then(setScan).catch(() => {})
    void refreshDiscovery()
  }, [refreshDiscovery])

  const cancelScan = useCallback(async () => {
    try {
      setScan(await petScanCancel())
      notify.info("正在取消扫描…")
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const removeDiscovery = useCallback(
    async (id: string) => {
      try {
        await petDiscoveryRemove(id)
        notify.info("已从发现列表移除")
        void refreshDiscovery()
      } catch (e) {
        notify.error(e instanceof Error ? e.message : String(e))
      }
    },
    [refreshDiscovery],
  )

  return {
    manifest,
    installed,
    applied,
    tasks,
    installErr,
    setInstallErr,
    detail,
    detailId,
    detailLoading,
    openDetail,
    closeDetail,
    startInstall,
    cancelTask,
    applySkin,
    uninstallSkin,
    scan,
    discovery,
    startScan,
    cancelScan,
    removeDiscovery,
    refreshDiscovery,
  }
}