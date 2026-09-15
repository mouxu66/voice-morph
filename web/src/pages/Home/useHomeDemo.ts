import { useCallback, useEffect, useRef, useState } from "react"
import {
  marketInstall,
  marketInstalled,
  marketManifest,
  marketPreviewStatus,
  marketPreviewTrigger,
  marketProgress,
  type MarketFileSlot,
  type MarketInstallState,
  type MarketItem,
  type MarketPreview,
  type MarketTask,
} from "@/api/client"

const AUDIO_RE = /\.(wav|mp3|flac|m4a|ogg|saf|aiff)$/i

export type HomeInstallState = {
  voiceId: string
  phase: string // 挂起中 / 下载模型 / 转换安装 / 已完成 / 失败
  percent: number
  error: string
}

/**
 * 首页「预置音色 · 即点即听」专用 hook —— 刻意比市场页瘦身：
 * 只拉精选清单 + 已装列表 + 试听状态，不引安装队列 / 进度轮询 / 搜索。
 */
export function useHomeDemo() {
  const [items, setItems] = useState<MarketItem[] | null>(null)
  const [installed, setInstalled] = useState<string[]>([])
  const [previews, setPreviews] = useState<Record<string, MarketPreview>>({})
  const previewLocks = useRef(new Set<string>())

  const refreshInstalled = useCallback(async () => {
    try {
      setInstalled(await marketInstalled())
    } catch {
      /* 后端离线时忽略 */
    }
  }, [])

  useEffect(() => {
    let alive = true
    void marketManifest()
      .then((m) => {
        if (alive) setItems(m)
      })
      .catch(() => {
        if (alive) setItems([])
      })
    void refreshInstalled()
    return () => {
      alive = false
    }
  }, [refreshInstalled])

  const isPlayable = useCallback((url?: string) => !!url && AUDIO_RE.test(url) && !/\.pth$/i.test(url), [])

  /** 生成（或取现成）一条试听：首次需下载模型再转换，可达数分钟；ready 后前端直播 url */
  const ensurePreview = useCallback(async (voice_id: string, download?: MarketFileSlot | null) => {
    if (!voice_id || previewLocks.current.has(voice_id)) return
    previewLocks.current.add(voice_id)
    const put = (r: MarketPreview) => setPreviews((v) => ({ ...v, [voice_id]: r }))
    try {
      let r = await marketPreviewStatus(voice_id)
      if (r.status === "ready" || r.status === "failed") {
        put(r)
        return
      }
      put({ status: "generating", url: "", error: "" })
      try {
        r = await marketPreviewTrigger(voice_id, download ?? undefined)
      } catch (e) {
        put({ status: "failed", url: "", error: e instanceof Error ? e.message : "试听生成启动失败" })
        return
      }
      const maxTries = download ? 300 : 90 // 带下载的试听最长 ~10 分钟；纯转换 ~3 分钟
      for (let i = 0; i < maxTries; i++) {
        await new Promise((res) => setTimeout(res, 2000))
        try {
          r = await marketPreviewStatus(voice_id)
        } catch {
          r = { status: "missing", url: "", error: "" }
        }
        if (r.status === "ready" || r.status === "failed") {
          put(r)
          return
        }
        if (r.status === "skipped") {
          put(r) // 如实显示"等待中"，卡片可手动重试
          return
        }
      }
      put({ status: "failed", url: "", error: "试听生成超时，请稍后重试" })
    } catch (e) {
      put({ status: "failed", url: "", error: e instanceof Error ? e.message : "试听生成失败" })
    } finally {
      previewLocks.current.delete(voice_id)
    }
  }, [])

  /**
   * 点一张音色卡：仓库自带演示音频直接播；否则解析可用直链（清单 / prefs）触发生成。
   * 没有可解析直链的条目静默忽略（首页暂不做快速槽兜底）。
   */
  const triggerPreview = useCallback(
    (item: MarketItem) => {
      if (isPlayable(item.demo)) return // 自带演示音频，直接播，不走生成
      const prefs = item.prefs
      const vid = prefs?.voice_id ?? item.voice_id ?? ""
      const dl: MarketFileSlot | null = prefs?.download
        ? { url: prefs.download.url, mirror_url: prefs.download.mirror_url, sha256: prefs.download.sha256 }
        : item.download
          ? { url: item.download.url, mirror_url: item.download.mirror_url, sha256: item.download.sha256 }
          : null
      if (vid && dl) {
        void ensurePreview(vid, dl)
      }
    },
    [ensurePreview, isPlayable],
  )

  // ---- 一键安装（预置音色 → 装好立即可用）----
  const [installing, setInstalling] = useState<HomeInstallState | null>(null)
  const installLock = useRef(false)

  /** 解析清单条目可用的权重直链（同上）。无 dl 时调用方应走 prefs 分支，这里返回 null。 */
  const installDl = useCallback((item: MarketItem): MarketFileSlot | null => {
    const prefs = item.prefs
    return prefs?.download
      ? { url: prefs.download.url, mirror_url: prefs.download.mirror_url, sha256: prefs.download.sha256 }
      : item.download
        ? { url: item.download.url, mirror_url: item.download.mirror_url, sha256: item.download.sha256 }
        : null
  }, [])

  /** 后端任务两种形态（task.install 或 task 本体），归一成页面可读的平面对象 */
  const flatInstall = useCallback((t: MarketTask | MarketInstallState | null | undefined) => {
    const s = t && "install" in t ? t.install : (t as MarketInstallState | null)
    if (!s) return { phase: "", percent: 0, status: "", error: "" }
    return {
      phase: s.phase ?? "",
      percent: s.percent ?? 0,
      status: (s.status ?? "") as string,
      error: s.error ?? "",
    }
  }, [])

  /**
   * 一键安装一个预置音色：走后端全局 FIFO 安装队列（与市场页同源）。
   * 返回 true=装好 / false=失败或已取消；调用方凭返回值决定跳转还是留在原地提示。
   * 重复点击（已装 / 正在装）自动忽略。
   */
  const installVoice = useCallback(
    async (item: MarketItem): Promise<boolean> => {
      const prefs = item.prefs
      const vid = prefs?.voice_id ?? item.voice_id ?? ""
      const dl = installDl(item)
      if (!vid || !dl) return false
      if (installed.includes(vid)) return true // 已装：直接算成功，交给调用方跳转
      if (installLock.current) return false
      installLock.current = true
      setInstalling({ voiceId: vid, phase: "排队中…", percent: 0, error: "" })
      try {
        const { task } = await marketInstall({
          voice_id: vid,
          download: dl,
          index: prefs?.index ?? item.index ?? undefined,
          display_name: prefs?.name ?? item.name,
          manifest_id: prefs?.id ?? item.id ?? "",
          overwrite: false,
        })
        const init = flatInstall(task)
        setInstalling({
          voiceId: vid,
          phase: init.phase || "下载安装中…",
          percent: init.percent,
          error: "",
        })
        // 后端安装是长任务，轮询进度；完成/失败即返回
        for (let i = 0; i < 600; i++) {
          if (!installLock.current) return false // 已取消
          await new Promise((res) => setTimeout(res, 2000))
          let cur = init
          try {
            const r = await marketProgress()
            const f = flatInstall(r.task)
            if (f.status || f.phase) cur = f
          } catch {
            continue
          }
          setInstalling({ voiceId: vid, phase: cur.phase || "下载安装中…", percent: cur.percent, error: cur.error })
          if (cur.status === "installed" || cur.status === "done") {
            await refreshInstalled()
            setInstalling(null)
            installLock.current = false
            return true
          }
          if (cur.status === "failed") {
            setInstalling({ voiceId: vid, phase: "安装失败", percent: 0, error: cur.error || "未知错误" })
            installLock.current = false
            return false
          }
        }
        setInstalling({ voiceId: vid, phase: "安装超时", percent: 0, error: "" })
        installLock.current = false
        return false
      } catch (e) {
        setInstalling({
          voiceId: vid,
          phase: "启动失败",
          percent: 0,
          error: e instanceof Error ? e.message : "安装启动失败",
        })
        installLock.current = false
        return false
      }
    },
    [installed, installDl, flatInstall, refreshInstalled],
  )

  return { items, installed, previews, ensurePreview, triggerPreview, isPlayable, installing, installVoice }
}