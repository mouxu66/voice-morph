import { useCallback, useEffect, useRef, useState } from "react"
import {
  auditionPicks,
  blindtestClose,
  blindtestCreate,
  blindtestDelete,
  blindtestDetail,
  blindtestSessions,
  getLan,
  type AuditionPick,
  type BlindtestDetail,
  type BlindtestSessionSummary,
} from "@/api/client"

/**
 * 「盲听评测」页的状态与动作。
 *
 * 数据流：
 * - 会话列表 + 选中会话详情：挂载拉一次；详情开着且未收卷时每 4s 轮询
 *   （评委在扫码打分，主办方这头要"活"起来）。
 * - 创建：样本 = 上传文件 + 试音间结果引用（ref 是 outputs/ 相对路径），
 *   每个样本一条可编辑的标签（主办方视角的名字，手机端永远看不到）。
 * - 盲听链接：复用 `/api/system/lan`（遥控二维码同一数据源），把 remote.html
 *   换成 listen.html；`?api_key=`（若设了 token）从 lan.url 里原样搬过来。
 */

/** 一条待录入的样本：file（本机上传）或 ref（服务器产物）。 */
export type SampleDraft = {
  key: string
  kind: "file" | "ref"
  /** 展示名：文件名 / 试音间音色名 */
  name: string
  /** ref 时的 outputs/ 相对路径 */
  ref?: string
  file?: File
  label: string
}

let draftSeq = 0
function nextKey(): string {
  draftSeq += 1
  return `d${Date.now()}_${draftSeq}`
}

/** 试音间产物 url（/api/media/outputs/audition/x.wav）→ outputs 相对路径。 */
export function auditionRefFromUrl(url: string): string | null {
  const m = /\/api\/media\/outputs\/(.+)$/.exec(url)
  return m ? decodeURIComponent(m[1]) : null
}

export function useBlindTest() {
  const [sessions, setSessions] = useState<BlindtestSessionSummary[]>([])
  const [selectedId, setSelectedId] = useState("")
  const [detail, setDetail] = useState<BlindtestDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [feedback, setFeedback] = useState("")

  // —— 创建表单 ——
  const [title, setTitle] = useState("")
  const [drafts, setDrafts] = useState<SampleDraft[]>([])
  const [creating, setCreating] = useState(false)

  // —— 试音间导入 ——
  const [picks, setPicks] = useState<AuditionPick[] | null>(null)
  const [picksOpen, setPicksOpen] = useState(false)
  const [picksError, setPicksError] = useState("")

  // —— 链接 ——
  const [lanUrl, setLanUrl] = useState<string | null>(null)

  const pollRef = useRef(true)

  const refreshSessions = useCallback(async (): Promise<BlindtestSessionSummary[]> => {
    try {
      const res = await blindtestSessions()
      setSessions(res.sessions)
      setError("")
      return res.sessions
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      return []
    } finally {
      setLoading(false)
    }
  }, [])

  const refreshDetail = useCallback(async (id: string): Promise<void> => {
    if (!id) return
    try {
      const d = await blindtestDetail(id)
      setDetail(d)
      setError("")
    } catch {
      // 会话被删等情况：清掉选中态
      setDetail(null)
      setSelectedId("")
    }
  }, [])

  // 挂载：列表 + 局域网地址（二维码数据源）
  useEffect(() => {
    pollRef.current = true
    void refreshSessions()
    getLan()
      .then((lan) => setLanUrl(lan.url))
      .catch(() => setLanUrl(null))
    return () => {
      pollRef.current = false
    }
  }, [refreshSessions])

  // 选中会话：拉一次详情；未收卷则持续轮询（评委打分进行中）
  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    void refreshDetail(selectedId)
    const t = setInterval(() => {
      if (pollRef.current) void refreshDetail(selectedId)
    }, 4000)
    return () => clearInterval(t)
  }, [selectedId, refreshDetail])

  /** 盲听链接（token + 局域网地址 → 二维码内容）。 */
  const listenUrl = useCallback(
    (token: string): string | null => {
      if (!lanUrl) return null
      try {
        const u = new URL(lanUrl)
        u.pathname = "/listen.html"
        const apiKey = u.searchParams.get("api_key")
        const params = new URLSearchParams({ t: token })
        if (apiKey) params.set("api_key", apiKey)
        u.search = params.toString()
        return u.toString()
      } catch {
        return null
      }
    },
    [lanUrl]
  )

  // —— 创建表单动作 ——

  const addFiles = useCallback((files: File[]) => {
    setDrafts((prev) => {
      const room = 10 - prev.length
      const add = files.slice(0, Math.max(0, room)).map((f) => ({
        key: nextKey(),
        kind: "file" as const,
        name: f.name,
        file: f,
        label: f.name.replace(/\.[^.]+$/, ""),
      }))
      return [...prev, ...add]
    })
  }, [])

  const openPicks = useCallback(async () => {
    setPicksOpen(true)
    setPicksError("")
    if (picks !== null) return
    try {
      const list = await auditionPicks()
      setPicks(list)
    } catch (e) {
      setPicksError(e instanceof Error ? e.message : String(e))
    }
  }, [picks])

  const addPick = useCallback((pick: AuditionPick) => {
    const ref = auditionRefFromUrl(pick.url)
    if (!ref) {
      setPicksError("这条试音结果的路径认不出来，跳过了")
      return
    }
    setDrafts((prev) => {
      if (prev.some((d) => d.ref === ref) || prev.length >= 10) return prev
      return [
        ...prev,
        {
          key: nextKey(),
          kind: "ref",
          name: pick.display_name,
          ref,
          label: pick.display_name,
        },
      ]
    })
  }, [])

  const removeDraft = useCallback((key: string) => {
    setDrafts((prev) => prev.filter((d) => d.key !== key))
  }, [])

  const setLabel = useCallback((key: string, label: string) => {
    setDrafts((prev) => prev.map((d) => (d.key === key ? { ...d, label } : d)))
  }, [])

  const create = useCallback(async () => {
    if (drafts.length < 2) {
      setError("至少要 2 个样本才能盲听对比")
      return
    }
    setCreating(true)
    setError("")
    setFeedback("")
    try {
      const files = drafts.filter((d) => d.kind === "file" && d.file).map((d) => d.file as File)
      const refs = drafts.filter((d) => d.kind === "ref" && d.ref).map((d) => d.ref as string)
      const labels = drafts.map((d) => d.label.trim() || d.name)
      const res = await blindtestCreate(title.trim() || "未命名盲听", labels, files, refs)
      setDrafts([])
      setTitle("")
      setFeedback("盲听会话已创建，把二维码发给评委吧")
      await refreshSessions()
      setSelectedId(res.id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setCreating(false)
    }
  }, [drafts, title, refreshSessions])

  // —— 会话动作 ——

  const closeSession = useCallback(
    async (id: string, closed: boolean) => {
      try {
        await blindtestClose(id, closed)
        setFeedback(closed ? "已收卷，评委不能再提交" : "已重新开放打分")
        await refreshSessions()
        if (selectedId === id) await refreshDetail(id)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [selectedId, refreshSessions, refreshDetail]
  )

  const removeSession = useCallback(
    async (id: string) => {
      try {
        await blindtestDelete(id)
        setFeedback("会话已删除（样本副本一并清理）")
        if (selectedId === id) setSelectedId("")
        await refreshSessions()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [selectedId, refreshSessions]
  )

  return {
    // 列表与详情
    sessions,
    selectedId,
    setSelectedId,
    detail,
    loading,
    error,
    feedback,
    // 创建
    title,
    setTitle,
    drafts,
    addFiles,
    removeDraft,
    setLabel,
    creating,
    create,
    // 试音间导入
    picks,
    picksOpen,
    picksError,
    openPicks,
    setPicksOpen,
    addPick,
    // 链接
    listenUrl,
    // 动作
    closeSession,
    removeSession,
    refreshSessions,
  }
}
