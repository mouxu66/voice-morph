import { useCallback, useEffect, useState } from "react"

import { fetchYtdlp, mediaUrl, ytdlpStatus, type YtdlpStatus } from "@/api/client"
import { friendlyError } from "@/lib/errors"

/**
 * 「在线扒歌」页的状态。
 *
 * 这个页面**不跑翻唱链路** —— 它只负责"把平台链接变成会话目录里的一个音频文件"。
 * 拉回来之后用户去「翻唱」页用它（产物都在会话目录里，同一个 `src_name` 口径）。
 *
 * 为什么拆成独立页而不是塞进「翻唱」页的第三个输入框：
 *   1. 「翻唱」页要**先确认拉对了歌**才能开跑（那页的三个来源已经够挤）；
 *   2. 这里要展示"yt-dlp 装没装、支持哪些站点"这类**环境信息**，
 *      塞进翻唱页会把它变成"环境排查页"；
 *   3. 关掉这个插件时，「翻唱」页的上传与粘直链两条路必须**完全不受影响** ——
 *      独立页让这个边界在代码层面就成立。
 */
export function useYtdlp() {
  const [status, setStatus] = useState<YtdlpStatus | null>(null)
  /** 首次探活中。用来区分"还没查"和"查完了但没有" */
  const [probing, setProbing] = useState(true)
  const [url, setUrl] = useState("")
  const [fetching, setFetching] = useState(false)
  const [errorMessage, setErrorMessage] = useState("")
  const [feedback, setFeedback] = useState("")
  /** 拉回来的音频：preview 是可试听的完整地址，summary 是"多大/多长"那一行 */
  const [preview, setPreview] = useState("")
  const [summary, setSummary] = useState("")
  const [fetchedName, setFetchedName] = useState("")
  const [fetchedSite, setFetchedSite] = useState("")
  /**
   * 后端**实际**交给 yt-dlp 的地址。
   *
   * 用户粘短链时它和输入不一样（后端会把 `c6.y.qq.com/base/fcgi-bin/u?__=…`
   * 规范化成 `y.qq.com/n/ryqq/songDetail/<mid>`）。界面只在两者不同时展示它 ——
   * "这条链接为什么能/不能拉"的答案就在这一行里。
   */
  const [fetchedSourceUrl, setFetchedSourceUrl] = useState("")
  /**
   * 本次成功拉取时**提交的原始输入**。
   *
   * 页面拿它和 `fetchedSourceUrl` 比，决定要不要显示"实际解析到 …"那一行。
   * 不能拿输入框当前值去比 —— 用户在结果出来后接着改输入框（很常见的动作），
   * 那一行就会莫名其妙地出现/消失。
   */
  const [submittedUrl, setSubmittedUrl] = useState("")

  // 探活：`/ytdlp/status` 是纯本地调用（不联网），只在进页面时查一次。
  // 但**必须能手动重查** —— 用户的真实路径是"看到没装 → 去装 → 回来"，
  // 装完不重启后端也想立刻看到变化。`probeNonce` 就是那个重查开关。
  const [probeNonce, setProbeNonce] = useState(0)

  useEffect(() => {
    let alive = true
    setProbing(true)
    void (async () => {
      try {
        const s = await ytdlpStatus()
        if (alive) setStatus(s)
      } catch (e) {
        if (alive) setStatus(null)
        // 探活失败不弹错误：页面会显示"未检测到"，够清楚了。
        // 这里吞掉是刻意的 —— 一个环境探测的失败不该盖住用户真正关心的输入框。
        void e
      } finally {
        if (alive) setProbing(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [probeNonce])

  /** 重查 yt-dlp 是否就位（装完之后不用重启） */
  const reprobe = useCallback(() => setProbeNonce((n) => n + 1), [])

  const fetchFromUrl = useCallback(async () => {
    const want = url.trim()
    if (!want) {
      setErrorMessage("请先粘一条歌曲链接")
      return
    }
    setErrorMessage("")
    setFeedback("")
    setFetching(true)
    try {
      const r = await fetchYtdlp(want)
      setFetchedName(r.name)
      setFetchedSite(r.site)
      setFetchedSourceUrl(r.source_url || "")
      setSubmittedUrl(want)
      setPreview(mediaUrl(r.url))
      setSummary(
        `${(r.bytes / 1024 / 1024).toFixed(1)} MB` +
          (r.duration_s
            ? ` · ${Math.floor(r.duration_s / 60)}:${String(Math.round(r.duration_s % 60)).padStart(2, "0")}`
            : "")
      )
      setFeedback(`已从${r.site}拉到 ${r.name}，先试听确认是不是这首歌`)
    } catch (e) {
      // 失败要把上一条成功的产物清掉 —— 否则界面上留着旧音频，用户会以为是这次拉的
      setFetchedName("")
      setFetchedSite("")
      setFetchedSourceUrl("")
      setSubmittedUrl("")
      setPreview("")
      setSummary("")
      setErrorMessage(friendlyError(e, "拉取失败"))
    } finally {
      setFetching(false)
    }
  }, [url])

  const clear = useCallback(() => {
    setFetchedName("")
    setFetchedSite("")
    setFetchedSourceUrl("")
    setSubmittedUrl("")
    setPreview("")
    setSummary("")
    setFeedback("")
    setErrorMessage("")
  }, [])

  return {
    status,
    probing,
    reprobe,
    url,
    setUrl,
    fetching,
    fetchFromUrl,
    errorMessage,
    feedback,
    preview,
    summary,
    fetchedName,
    fetchedSite,
    fetchedSourceUrl,
    submittedUrl,
    clear,
    /** yt-dlp 找到没。没找到时页面显示"怎么装"，输入框禁用 */
    ready: Boolean(status?.available),
  }
}
