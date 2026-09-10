import { useCallback, useEffect, useRef, useState } from "react"
import {
  petApplied,
  petApply,
  petInstall,
  petInstalled,
  petManifest,
  petProgress,
  petUninstall,
  type PetAppliedSkin,
  type PetInstalledItem,
  type PetSkinItem,
  type PetTask,
} from "@/api/client"
import { notify } from "@/lib/notify"

/** 进行中的状态集合（其余为终结态） */
const ACTIVE = new Set(["downloading", "installing"])

export type PetMarket = ReturnType<typeof usePetMarket>

export function usePetMarket() {
  const [manifest, setManifest] = useState<PetSkinItem[] | null>(null)
  const [installed, setInstalled] = useState<PetInstalledItem[]>([])
  const [applied, setApplied] = useState<PetAppliedSkin | null>(null)
  const [task, setTask] = useState<PetTask | null>(null)
  const [installErr, setInstallErr] = useState("")
  const prevStatus = useRef("")
  const busyRef = useRef(false)   // 轮询慢响应不叠加

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

  // 轮询安装进度：1s 一次；状态由进行中 → 终结时刷新已装列表
  useEffect(() => {
    let alive = true
    const timer = window.setInterval(async () => {
      if (busyRef.current) return
      busyRef.current = true
      try {
        const t = await petProgress()
        if (!alive) return
        setTask(t)
        const cur = t?.status ?? ""
        const wasActive = ACTIVE.has(prevStatus.current)
        prevStatus.current = cur
        if (cur && !ACTIVE.has(cur) && (wasActive || cur === "failed")) {
          if (cur === "failed" && t?.error) setInstallErr(t.error)
          else if (cur === "done") {
            const name = t.skin_id || "皮肤"
            notify.success(`「${name}」皮肤安装完成`)
          }
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

  // 安装 / 应用 / 卸载
  const startInstall = useCallback(async (id: string) => {
    setInstallErr("")
    try {
      await petInstall(id)
    } catch (e) {
      setInstallErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const applySkin = useCallback(
    async (id: string) => {
      try {
        await petApply(id)
        notify.success("皮肤已应用，桌面人偶会在几秒内换上新外观")
        void refreshApplied()
      } catch (e) {
        notify.error(e instanceof Error ? e.message : String(e))
      }
    },
    [refreshApplied],
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

  return {
    manifest,
    installed,
    applied,
    task,
    installErr,
    setInstallErr,
    startInstall,
    applySkin,
    uninstallSkin,
  }
}