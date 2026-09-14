import { useCallback, useEffect, useRef, useState } from "react"
import {
  marketInstalled,
  marketManifest,
  marketPreviewStatus,
  marketPreviewTrigger,
  type MarketFileSlot,
  type MarketItem,
  type MarketPreview,
} from "@/api/client"

const AUDIO_RE = /\.(wav|mp3|flac|m4a|ogg|saf|aiff)$/i

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

  return { items, installed, previews, ensurePreview, triggerPreview, isPlayable }
}