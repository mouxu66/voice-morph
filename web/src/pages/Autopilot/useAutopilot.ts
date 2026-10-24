import { useCallback, useEffect, useRef, useState } from "react"

import {
  autopilotPause,
  autopilotResume,
  autopilotSaveConfig,
  autopilotSessions,
  autopilotStart,
  autopilotState,
  autopilotStop,
  autopilotTestSend,
  type AutopilotConfig,
  type AutopilotSession,
  type AutopilotState,
} from "@/api/client"

/** 状态轮询间隔。会话列表走 UIA（实测 ~50ms），5s 足够且不打扰用户操作微信。 */
const POLL_MS = 5000

function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** 白名单增删（纯函数，页面与测试共用）。 */
export function withChat(cfg: AutopilotConfig, chat: string, on: boolean): AutopilotConfig {
  const list = on ? [...cfg.whitelist, chat] : cfg.whitelist.filter((x) => x !== chat)
  const groups = on ? cfg.groups : cfg.groups.filter((x) => x !== chat)
  return { ...cfg, whitelist: Array.from(new Set(list)), groups }
}

/** 群聊标记增删。只有已在白名单里的会话才允许标记。 */
export function withGroup(cfg: AutopilotConfig, chat: string, on: boolean): AutopilotConfig {
  if (!cfg.whitelist.includes(chat)) return cfg
  return { ...cfg, groups: on ? Array.from(new Set([...cfg.groups, chat])) : cfg.groups.filter((x) => x !== chat) }
}

/** 能不能点「开始托管」：必须已授权、白名单非空、且当前没在跑。 */
export function canStart(state: AutopilotState | null, consent: boolean, cfg: AutopilotConfig | null): boolean {
  if (!state || !cfg) return false
  if (state.status !== "off") return false
  return consent && cfg.whitelist.length > 0
}

export function useAutopilot() {
  const [state, setState] = useState<AutopilotState | null>(null)
  const [sessions, setSessions] = useState<AutopilotSession[]>([])
  const [sessionsError, setSessionsError] = useState("")
  const [draft, setDraft] = useState<AutopilotConfig | null>(null)
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")

  // 用户改过配置但没保存时，轮询不能把草稿冲掉
  const dirty = useRef(false)

  const load = useCallback(async () => {
    try {
      const st = await autopilotState()
      setState(st)
      if (!dirty.current) setDraft(st.config)
      setError("")
    } catch (e) {
      setError(msg(e))
    }
    try {
      const s = await autopilotSessions()
      setSessions(s.sessions ?? [])
      setSessionsError(s.ok ? "" : s.error || "读不到会话列表")
    } catch (e) {
      setSessionsError(msg(e))
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), POLL_MS)
    return () => clearInterval(timer)
  }, [load])

  const patch = useCallback((next: AutopilotConfig) => {
    dirty.current = true
    setDraft(next)
    setNotice("")
  }, [])

  const run = useCallback(async (tag: string, fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(tag)
    setError("")
    setNotice("")
    try {
      await fn()
      setNotice(okMsg)
    } catch (e) {
      setError(msg(e))
    } finally {
      setBusy("")
      await load()
    }
  }, [load])

  const save = useCallback(async () => {
    if (!draft) return
    setBusy("save")
    setError("")
    setNotice("")
    try {
      const res = await autopilotSaveConfig(draft)
      dirty.current = false
      setDraft(res.config)
      setNotice("配置已保存")
    } catch (e) {
      setError(msg(e))
    } finally {
      setBusy("")
      await load()
    }
  }, [draft, load])

  const start = useCallback(() => run("start", () => autopilotStart(consent), "托管已开启"), [run, consent])
  const pause = useCallback(() => run("pause", () => autopilotPause(), "已暂停"), [run])
  const resume = useCallback(() => run("resume", () => autopilotResume(), "已恢复"), [run])
  const stop = useCallback(() => run("stop", () => autopilotStop(), "已停止，授权需要下次重新开启"), [run])

  const testSend = useCallback(
    (text: string) => run("test", () => autopilotTestSend(text), "已发到「文件传输助手」，去微信看看"),
    [run],
  )

  return {
    state,
    sessions,
    sessionsError,
    draft,
    consent,
    setConsent,
    busy,
    error,
    notice,
    setNotice,
    load,
    patch,
    save,
    start,
    pause,
    resume,
    stop,
    testSend,
    running: state?.status === "running",
    paused: state?.status === "paused",
  }
}
