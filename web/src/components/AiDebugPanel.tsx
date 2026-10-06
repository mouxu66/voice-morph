import { useCallback, useEffect, useState } from "react"
import { Bot, CheckCircle2, History, Lock, RefreshCw, X, XCircle } from "lucide-react"
import { getAiActions, getAiAudit } from "@/api/client"
import type { AiActionsCatalog, AiAuditEntry } from "@/api/client"

/**
 * AI 调试面面板：动作目录 + AI 操作记录（第二期）。
 *
 * 两条设计口径（与 `docs/AI调试端点设计.md` 一致，改动前先读）：
 *
 * 1. **拒绝也要说清为什么**：写能力没开时展示 `missing_gates` 逐条点名
 *    （缺哪个环境变量），而不是笼统一句「未启用」—— 用户照着就能开。
 * 2. **关掉写能力 ≠ 销毁证据**：面板**不受 `hook.ai` 启用状态门控**。
 *    关插件是为了止血，不是为了抹掉「AI 之前改过什么」的记录；同理
 *    `GET /api/ai/audit` 归 `core.system`，插件关着照样读得到。
 */
export function AiDebugPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [catalog, setCatalog] = useState<AiActionsCatalog | null>(null)
  const [entries, setEntries] = useState<AiAuditEntry[]>([])
  const [auditNote, setAuditNote] = useState("")
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setErr(null)
    try {
      const [cat, tail] = await Promise.all([getAiActions(), getAiAudit(100)])
      setCatalog(cat)
      setEntries(tail.items)
      setAuditNote(tail.note)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
      // 后端不可达时把两个面板都清空：留着上一次的数据、却配上
      // 「写能力未启用」的提示，会让人以为那是当前状态。
      setCatalog(null)
      setEntries([])
      setAuditNote("")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void reload()
  }, [open, reload])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
      role="dialog"
      aria-label="AI 调试面"
    >
      <div
        className="flex max-h-[85dvh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <Bot className="h-5 w-5 text-primary" />
            <h2 className="text-sm font-semibold text-foreground">AI 调试面</h2>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void reload()}
              disabled={loading}
              aria-label="刷新"
              className="rounded-md p-1.5 text-muted-foreground hover:bg-accent disabled:opacity-50"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded-md p-1.5 text-muted-foreground hover:bg-accent"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 text-sm">
          {err && (
            <p className="mb-4 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive">
              取不到 AI 调试面：{err}
            </p>
          )}

          {/* ---- 写能力状态 ---- */}
          <section className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              写动作状态
            </h3>
            {!catalog ? (
              <p className="text-muted-foreground">—</p>
            ) : catalog.write_enabled ? (
              <p className="flex items-start gap-2 rounded-lg border border-border bg-accent/40 px-3 py-2">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <span>{catalog.write_reason}</span>
              </p>
            ) : (
              <div className="rounded-lg border border-border px-3 py-2">
                <p className="flex items-start gap-2">
                  <Lock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  <span>{catalog.write_reason}</span>
                </p>
                {catalog.missing_gates.length > 0 && (
                  <ul className="mt-2 ml-6 list-disc space-y-0.5 text-xs text-muted-foreground">
                    {catalog.missing_gates.map((g) => (
                      <li key={g}>{g}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </section>

          {/* ---- 可用动作 ---- */}
          <section className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              可用动作（白名单）
            </h3>
            {!catalog || catalog.actions.length === 0 ? (
              <p className="text-muted-foreground">
                当前没有可用动作（写动作属可选插件 hook.ai，默认关闭）。
              </p>
            ) : (
              <ul className="space-y-2">
                {catalog.actions.map((a) => (
                  <li key={a.id} className="rounded-lg border border-border px-3 py-2">
                    <p className="flex items-center gap-2">
                      <code className="rounded bg-accent px-1.5 py-0.5 text-xs">{a.id}</code>
                      <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        风险 {a.risk}
                      </span>
                    </p>
                    <p className="mt-1 text-muted-foreground">{a.desc}</p>
                    <dl className="mt-1.5 space-y-0.5 text-xs">
                      {Object.entries(a.schema).map(([k, v]) => (
                        <div key={k} className="flex gap-2">
                          <dt className="shrink-0 text-muted-foreground">{k}</dt>
                          <dd className="text-muted-foreground">{v}</dd>
                        </div>
                      ))}
                    </dl>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ---- 明确拒绝 ---- */}
          <section className="mb-5">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              永不开放的动作
            </h3>
            {!catalog ? (
              <p className="text-muted-foreground">—</p>
            ) : (
              <ul className="space-y-1">
                {catalog.denied.map((d) => (
                  <li key={d.id} className="flex items-start gap-2 text-xs">
                    <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
                    <span>
                      <code className="mr-1.5">{d.id}</code>
                      <span className="text-muted-foreground">{d.reason}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* ---- AI 操作记录 ---- */}
          <section>
            <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <History className="h-3.5 w-3.5" />
              AI 操作记录
            </h3>
            {entries.length === 0 ? (
              <p className="text-muted-foreground">
                {auditNote || "还没有 AI 操作记录。写动作每次执行（含 dry-run）都会在这里留账。"}
              </p>
            ) : (
              <ul className="space-y-1.5">
                {entries.map((e) => (
                  <li
                    key={e.id}
                    className="rounded-lg border border-border px-2.5 py-1.5 text-xs"
                  >
                    <p className="flex flex-wrap items-center gap-1.5">
                      {e.ok ? (
                        <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-primary" />
                      ) : (
                        <XCircle className="h-3.5 w-3.5 shrink-0 text-destructive" />
                      )}
                      <code className="rounded bg-accent px-1 py-0.5">{e.action}</code>
                      {e.dry_run && (
                        <span className="rounded border border-border px-1 py-0.5 text-[10px] text-muted-foreground">
                          dry-run
                        </span>
                      )}
                      <span className="text-muted-foreground">{e.message}</span>
                    </p>
                    <p className="mt-0.5 ml-5 text-[11px] text-muted-foreground">
                      {new Date(e.ts * 1000).toLocaleString()} · code {e.code} ·{" "}
                      {e.duration_ms}ms · {e.actor}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {auditNote && entries.length > 0 && (
              <p className="mt-2 text-[11px] text-muted-foreground">{auditNote}</p>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}