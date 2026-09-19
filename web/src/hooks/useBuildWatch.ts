import { useCallback, useEffect, useState } from "react"
import { startBuildWatch, stopBuildWatch, getBuildWatchStatus, type BuildWatchStatus } from "@/lib/electron"

/**
 * 构建监听器 Hook
 * 
 * 提供：
 * - status: 当前状态
 * - start(): 启动监听器
 * - stop(): 停止监听器
 * - loading: 操作中的加载状态
 */
export function useBuildWatch() {
  const [status, setStatus] = useState<BuildWatchStatus | null>(null)
  const [loading, setLoading] = useState(false)
  
  // 查询状态
  const refreshStatus = useCallback(async () => {
    const s = await getBuildWatchStatus()
    if (s) setStatus(s)
  }, [])
  
  // 启动
  const start = useCallback(async () => {
    setLoading(true)
    try {
      const result = await startBuildWatch()
      if (result?.success) {
        await refreshStatus()
      }
      return result
    } catch (err) {
      console.error("[useBuildWatch] 启动失败:", err)
      return null
    } finally {
      setLoading(false)
    }
  }, [refreshStatus])
  
  // 停止
  const stop = useCallback(async () => {
    setLoading(true)
    try {
      const result = await stopBuildWatch()
      if (result?.success) {
        await refreshStatus()
      }
      return result
    } catch (err) {
      console.error("[useBuildWatch] 停止失败:", err)
      return null
    } finally {
      setLoading(false)
    }
  }, [refreshStatus])
  
  // 初始加载 + 窗口聚焦时刷新
  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])
  
  useEffect(() => {
    const handleFocus = () => void refreshStatus()
    window.addEventListener("focus", handleFocus)
    return () => window.removeEventListener("focus", handleFocus)
  }, [refreshStatus])
  
  return {
    status,
    start,
    stop,
    loading,
    refresh: refreshStatus,
  }
}
