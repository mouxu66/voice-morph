import { useCallback, useEffect, useRef, useState } from "react"

import {
  fetchYtdlp,
  mediaUrl,
  ytdlpCancel,
  ytdlpJob,
  ytdlpStatus,
  type YtdlpStatus,
} from "@/api/client"
import { friendlyError } from "@/lib/errors"

/**
 * 生成取回作业号。后端只认 `^[0-9A-Za-z_-]{8,64}$`，所以两条路都合规：
 * 优先用 `crypto.randomUUID()`（Chromium 与 Node 都有），拿不到再退回
 * "时间戳 + 随机后缀"（老 WebView / 某些 jsdom 里没有 `randomUUID`）。
 */
function newJobId(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === "function") return c.randomUUID()
  return `job_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
}

/** 用户主动取消是**正常动作**，不能当成"拉取失败"弹红条。 */
function isAbort(e: unknown): boolean {
  return Boolean(e) && typeof e === "object" && (e as { name?: string }).name === "AbortError"
}

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
   * 音质说明。**只在走了 MV 兜底时非空** —— 会员曲从官方 MV 抽的音轨，
   * 是混过影像声音的 192kbps AAC，不是母带。必须显示给用户看，
   * 不能让兜底产物在界面上和正版音源长得一样。
   */
  const [qualityNote, setQualityNote] = useState("")
  /**
   * 本次成功拉取时**提交的原始输入**。
   *
   * 页面拿它和 `fetchedSourceUrl` 比，决定要不要显示"实际解析到 …"那一行。
   * 不能拿输入框当前值去比 —— 用户在结果出来后接着改输入框（很常见的动作），
   * 那一行就会莫名其妙地出现/消失。
   */
  const [submittedUrl, setSubmittedUrl] = useState("")

  /**
   * 取回进度。`percent` 为 null 表示"还没读到进度行"（**别显示成 0%** ——
   * 那是"一点没下"，和"还不知道"是两回事）。
   */
  const [progress, setProgress] = useState<{ stage: string; percent: number | null }>({
    stage: "",
    percent: null,
  })
  /** 本次取回的作业号（取消要用它）。没在取回时是空串。 */
  const [jobId, setJobId] = useState("")
  /**
   * 正在飞的请求。用 ref 而不是 state：`cancel` 要能**立刻**abort 它，
   * 而 state 更新是异步的 —— 读到的是上一次渲染的值，取消就会晚一拍。
   */
  const flight = useRef<AbortController | null>(null)

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

  /**
   * 取回期间按秒问一次后端"到哪一步了"。
   *
   * 为什么需要：取回是同步阻塞的，没有流式进度 —— 不轮询的话前端只能转一个
   * 转不完的圈，用户无法区分"在慢慢下"和"早就卡住了"（改动前就是后者，最长干等
   * 十分钟）。轮询失败**刻意静默**：进度是锦上添花，不能因为它出错就说拉取失败。
   */
  useEffect(() => {
    if (!fetching || !jobId) return
    let alive = true
    const timer = setInterval(() => {
      void (async () => {
        try {
          const s = await ytdlpJob(jobId)
          if (alive && s.found) setProgress({ stage: s.stage, percent: s.percent })
        } catch {
          /* 见上：进度查不到不该打扰用户 */
        }
      })()
    }, 1000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [fetching, jobId])

  const fetchFromUrl = useCallback(async () => {
    const want = url.trim()
    if (!want) {
      setErrorMessage("请先粘一条歌曲链接")
      return
    }
    const jid = newJobId()
    const ctrl = new AbortController()
    flight.current = ctrl
    setErrorMessage("")
    setFeedback("")
    setFetching(true)
    setJobId(jid)
    setProgress({ stage: "准备中", percent: null })
    try {
      const r = await fetchYtdlp(want, jid, ctrl.signal)
      setFetchedName(r.name)
      setFetchedSite(r.site)
      setFetchedSourceUrl(r.source_url || "")
      setQualityNote(r.quality_note || "")
      setSubmittedUrl(want)
      setPreview(mediaUrl(r.url))
      setSummary(
        `${(r.bytes / 1024 / 1024).toFixed(1)} MB` +
          (r.duration_s
            ? ` · ${Math.floor(r.duration_s / 60)}:${String(Math.round(r.duration_s % 60)).padStart(2, "0")}`
            : "")
      )
      // 兜底产物的话术要和正版音源分开 —— 不能让用户以为拿到的是母带。
      setFeedback(
        r.via === "mv_fallback"
          ? `这首歌需要用会员才能取正版音源，已改从${r.site}抽音轨（音质有折损）。先试听确认是不是这首歌`
          : `已从${r.site}拉到 ${r.name}，先试听确认是不是这首歌`
      )
    } catch (e) {
      // 失败要把上一条成功的产物清掉 —— 否则界面上留着旧音频，用户会以为是这次拉的
      setFetchedName("")
      setFetchedSite("")
      setFetchedSourceUrl("")
      setQualityNote("")
      setSubmittedUrl("")
      setPreview("")
      setSummary("")
      if (isAbort(e)) {
        // 用户自己按的取消：给一句确认，别弹错误（那不是失败）
        setFeedback("已取消取回")
      } else {
        setErrorMessage(friendlyError(e, "拉取失败"))
      }
    } finally {
      if (flight.current === ctrl) flight.current = null
      setFetching(false)
      setJobId("")
      setProgress({ stage: "", percent: null })
    }
  }, [url])

  /**
   * 取消这次取回。
   *
   * 顺序是**先叫后端停、再 abort 本地请求**：反过来的话本地连接先断，
   * 那个取消请求可能根本没发出去。两条路其实是互补的 —— 后端也会在连接断开时
   * 自行取消，所以即便这一步失败了，yt-dlp 也不会留在后台空转（见 `ytdlp_api`）。
   */
  const cancel = useCallback(async () => {
    const jid = jobId
    if (!jid) return
    try {
      await ytdlpCancel(jid)
    } catch {
      // 取消失败不该弹错：本地 abort 与后端的断连兜底都还在
    }
    flight.current?.abort()
  }, [jobId])

  const clear = useCallback(() => {
    setFetchedName("")
    setFetchedSite("")
    setFetchedSourceUrl("")
    setQualityNote("")
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
    cancel,
    progress,
    errorMessage,
    feedback,
    preview,
    summary,
    fetchedName,
    fetchedSite,
    fetchedSourceUrl,
    qualityNote,
    submittedUrl,
    clear,
    /** yt-dlp 找到没。没找到时页面显示"怎么装"，输入框禁用 */
    ready: Boolean(status?.available),
  }
}
