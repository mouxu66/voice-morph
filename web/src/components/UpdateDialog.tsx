import { useCallback, useEffect, useState } from "react"
import { AlertCircle, ArrowUpRight, CheckCircle2, Download, Loader2, PackageCheck, RefreshCw, Sparkles, X } from "lucide-react"
import {
  checkUpdate,
  downloadUpdate,
  installUpdate,
  onUpdateProgress,
  skipUpdate,
  type UpdateCheck,
} from "@/lib/electron"

function formatSize(bytes?: number) {
  if (!bytes || bytes <= 0) return ""
  const mb = bytes / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`
}

/** 更新内容渲染：支持 `###` 小标题、`- ` 列表、空行分段（纯文本增强，不引三方 markdown 库） */
function NotesBlock({ notes }: { notes: string }) {
  const lines = String(notes || "").split(/\r?\n/)
  return (
    <div className="rounded-lg border border-border bg-background/60 px-3 py-2.5">
      {lines.map((line, i) => {
        const t = line.trim()
        if (!t) return <div key={i} className="h-1.5" />
        if (t.startsWith("#")) {
          return <p key={i} className="mt-1 text-xs font-semibold text-foreground first:mt-0">{t.replace(/^#+\s*/, "")}</p>
        }
        if (/^[-*]\s+/.test(t)) {
          return (
            <p key={i} className="flex gap-2 text-xs leading-5 text-muted-foreground">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-primary" />
              <span>{t.replace(/^[-*]\s+/, "")}</span>
            </p>
          )
        }
        return <p key={i} className="text-xs leading-5 text-muted-foreground">{t}</p>
      })}
    </div>
  )
}

type Phase = "checking" | "idle" | "downloading" | "ready" | "installing"

/** 更新页：版本号对比 + 更新内容 + 下载进度 + 安装。非桌面端由调用方决定是否展示入口。 */
export function UpdateDialog({ open, onClose, initialCheck }: {
  open: boolean
  onClose: () => void
  /** 启动静默检查已拿到结果时传入，避免重复请求 */
  initialCheck?: UpdateCheck | null
}) {
  const [phase, setPhase] = useState<Phase>("checking")
  const [result, setResult] = useState<UpdateCheck | null>(null)
  const [pct, setPct] = useState(0)
  const [file, setFile] = useState("")
  const [error, setError] = useState("")

  // 下载进度（主进程推来）
  useEffect(() => onUpdateProgress((p) => {
    setPct(p.pct)
    if (p.done) setPhase("ready")
  }), [])

  useEffect(() => {
    if (!open) return
    setError("")
    setPct(0)
    setFile("")
    if (initialCheck) {
      setResult(initialCheck)
      setPhase("idle")
      return
    }
    let alive = true
    setPhase("checking")
    void (async () => {
      const r = await checkUpdate()
      if (!alive) return
      if (!r) {
        setError("当前不是桌面端（或应用版本较旧），无法检查更新")
      }
      setResult(r)
      setPhase("idle")
    })()
    return () => { alive = false }
  }, [open, initialCheck])

  const handleDownload = useCallback(async () => {
    const manifest = result?.latest
    if (!manifest) return
    setError("")
    setPct(0)
    setPhase("downloading")
    const r = await downloadUpdate(manifest)
    if (!r) {
      setError("无法下载更新（非桌面端）")
      setPhase("idle")
      return
    }
    if (!r.ok) {
      setError(r.reason || "下载失败")
      setPhase("idle")
      return
    }
    setFile(r.file || "")
    setPhase("ready")
  }, [result])

  const handleInstall = useCallback(async () => {
    setPhase("installing")
    const r = await installUpdate(file)
    if (r && !r.ok) {
      setError(r.reason || "启动安装程序失败")
      setPhase("ready")
    }
    // 成功时应用会退出并由安装程序接管，无需处理
  }, [file])

  const handleSkip = useCallback(async () => {
    if (result?.latest?.version) await skipUpdate(result.latest.version)
    onClose()
  }, [result, onClose])

  if (!open) return null

  const latest = result?.latest
  const notConfigured = result && !result.configured
  const upToDate = result?.ok && !result.hasUpdate && result.configured
  const hasUpdate = Boolean(result?.hasUpdate && latest)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            {hasUpdate ? (
              <Sparkles className="h-5 w-5 text-primary" />
            ) : (
              <PackageCheck className="h-5 w-5 text-primary" />
            )}
            <h2 className="text-sm font-semibold text-foreground">检查更新</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 内容 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {phase === "checking" ? (
            <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在检查更新…
            </div>
          ) : error ? (
            <div className="flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-destructive">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <p className="text-xs leading-5">{error}</p>
            </div>
          ) : notConfigured ? (
            <div className="space-y-2 text-xs leading-5 text-muted-foreground">
              <p className="text-sm font-medium text-card-foreground">更新源已关闭</p>
              <p>当前是纯本地模式，不会发起任何网络请求（更新源被环境变量 <code className="rounded bg-background px-1 font-mono">VM_UPDATE_URL=off</code> 关掉了）。安装版默认从 GitHub Releases 检查更新；若你要用自建源，把 <code className="rounded bg-background px-1 font-mono">VM_UPDATE_URL</code> 指向自己的 <code className="rounded bg-background px-1 font-mono">latest.json</code> 即可。</p>
            </div>
          ) : upToDate ? (
            <div className="flex items-start gap-2.5 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2.5">
              <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
              <div>
                <p className="text-sm font-medium text-primary">已是最新版本</p>
                <p className="mt-1 text-xs text-muted-foreground">当前版本 v{result?.current}</p>
              </div>
            </div>
          ) : hasUpdate && latest ? (
            <div className="space-y-3">
              <div className="flex items-center gap-3 rounded-lg border border-primary/30 bg-primary/10 px-3 py-2.5">
                <span className="rounded-md bg-muted px-2 py-1 font-mono text-xs text-muted-foreground">v{result.current}</span>
                <ArrowUpRight className="h-4 w-4 text-primary" />
                <span className="rounded-md bg-primary px-2 py-1 font-mono text-xs font-medium text-primary-foreground">v{latest.version}</span>
                <span className="ml-auto text-xs text-muted-foreground">
                  {[latest.pub_date ? latest.pub_date.slice(0, 10) : "", formatSize(latest.size)].filter(Boolean).join(" · ")}
                </span>
              </div>

              <div>
                <p className="mb-1.5 text-xs font-medium text-card-foreground">更新内容</p>
                {latest.notes ? (
                  <NotesBlock notes={latest.notes} />
                ) : (
                  <p className="rounded-lg border border-border bg-background/60 px-3 py-2.5 text-xs text-muted-foreground">
                    本次未提供更新说明。
                  </p>
                )}
              </div>

              {phase === "downloading" && (
                <div className="space-y-1.5">
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span className="flex items-center gap-1.5"><Loader2 className="h-3.5 w-3.5 animate-spin" />下载中…</span>
                    <span className="font-mono">{pct}%</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: `${Math.max(2, pct)}%` }} />
                  </div>
                </div>
              )}
              {phase === "ready" && (
                <p className="flex items-center gap-1.5 text-xs text-primary">
                  <CheckCircle2 className="h-3.5 w-3.5" />下载完成，点「立即更新」安装并重启应用
                </p>
              )}
              {phase === "installing" && (
                <p className="flex items-center gap-1.5 text-xs text-primary">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />正在启动安装程序，应用即将退出…
                </p>
              )}
            </div>
          ) : (
            <div className="flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-destructive">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              <p className="text-xs leading-5">{result?.reason || "检查更新失败"}</p>
            </div>
          )}
        </div>

        {/* 底部操作 */}
        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <div className="flex items-center gap-2">
            {hasUpdate && latest && !latest.mandatory && phase === "idle" && (
              <button
                type="button"
                onClick={() => void handleSkip()}
                className="rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground"
              >
                跳过此版本
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                setError("")
                setPhase("checking")
                void (async () => {
                  const r = await checkUpdate()
                  setResult(r)
                  if (!r) setError("当前不是桌面端（或应用版本较旧），无法检查更新")
                  setPhase("idle")
                })()
              }}
              disabled={phase === "checking" || phase === "downloading"}
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${phase === "checking" ? "animate-spin" : ""}`} /> 重新检查
            </button>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={phase === "installing"}
              className="rounded-md border border-border bg-background px-3 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              稍后
            </button>
            {hasUpdate && phase !== "ready" && (
              <button
                type="button"
                onClick={() => void handleDownload()}
                disabled={phase === "downloading" || phase === "installing"}
                className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-60"
              >
                {phase === "downloading" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                {phase === "downloading" ? "下载中" : "下载更新"}
              </button>
            )}
            {hasUpdate && phase === "ready" && (
              <button
                type="button"
                onClick={() => void handleInstall()}
                className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:bg-primary/90"
              >
                <PackageCheck className="h-3.5 w-3.5" /> 立即更新
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
