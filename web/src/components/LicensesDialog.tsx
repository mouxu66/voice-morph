import { useCallback, useEffect, useState } from "react"
import { ChevronDown, FileText, Loader2, RefreshCw, Scale, X } from "lucide-react"
import { backendPrefix } from "@/api/client"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

/** 载荷清单里的一条组件（结构由 tools/sync_license_payload.py 生成，见其 docstring）。 */
type Component = {
  id: string
  name: string
  version?: string
  license: string
  copyright: string
  file: string
  distributed: string
  scope: string
}

/**
 * 许可原文载荷的 HTTP 前缀。
 *
 * 三种运行模式的解析规则与 `backendPrefix()` 完全一致，不能另写一套：
 *   - dev（Vite 5173）："" → vite 直接吐 `public/licenses/…`
 *   - 打包态（file://）：`http://127.0.0.1:8000` → 后端 SPA catch-all 吐 `web_dist/licenses/…`
 *   - 局域网（手机浏览器开 `http://<PC-IP>:8000`）："" → 同上，同源
 *
 * 所以许可原文在三种模式下**读的是同一份文件**，不存在"桌面端看得到、局域网看不到"。
 */
function licensesBase(): string {
  return `${backendPrefix()}/licenses`
}

/**
 * 开源许可页：把**随发行物分发的许可原文**摆到用户面前。
 *
 * 为什么要有这个界面（而不是把 LICENSE 文件往安装包里一塞了事）：
 *   - OFL-1.1 §1 要求再分发字体时**附带版权声明与许可**，"附了但用户找不到"不算履行；
 *   - `THIRD_PARTY_NOTICES.md` 在仓库里，装完桌面端的用户手里没有仓库 —— 界面上看不到
 *     就等于没告知。
 *
 * 内容范围**刻意只为"随包分发的许可原文"**：运行时依赖、模型权重那些不随包走的部分
 * 不在这里重列，只给一行指向仓库清单的说明。理由见 `tools/audit_licenses.py` 的
 * docstring —— 同一个事实写两处必然漂移，而漂移的许可文档比没有更危险。
 */
export function LicensesDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [components, setComponents] = useState<Component[] | null>(null)
  const [texts, setTexts] = useState<Record<string, string>>({})
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      const base = licensesBase()
      const res = await fetch(`${base}/index.json`, { cache: "no-store" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as { components?: Component[] }
      const list = Array.isArray(data.components) ? data.components : []
      setComponents(list)
      // 原文一次拉齐：合计不到 20KB，换掉"点了才转圈"的交互成本
      const pairs = await Promise.all(
        list.map(async (c) => {
          const r = await fetch(`${base}/${c.file}`, { cache: "no-store" })
          return [c.file, r.ok ? await r.text() : `（读取失败：HTTP ${r.status}）`] as const
        }),
      )
      setTexts(Object.fromEntries(pairs))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setComponents(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="开源许可"
    >
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-2xl border border-border bg-card p-5 shadow-2xl sm:p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="font-mono text-xs uppercase tracking-widest text-primary">OPEN SOURCE</p>
            <h3 className="mt-1.5 flex items-center gap-2 text-lg font-semibold text-card-foreground">
              <Scale className="h-4 w-4 text-primary" />
              开源许可
            </h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              以下是随本安装包一起分发的第三方组件及其许可原文。点标题展开全文。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              className="rounded-md p-2 text-muted-foreground transition hover:bg-primary/10 hover:text-primary"
              aria-label="重新读取"
            >
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md p-2 text-muted-foreground transition hover:bg-destructive/10 hover:text-destructive"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {error && <ErrorPanel title="许可原文读取失败" detail={error} className="mt-3" />}

        <div className="mt-4 min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
          {loading && !components && (
            <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在读取许可原文…
            </p>
          )}

          {(components ?? []).map((c) => {
            const isOpen = expanded === c.id
            const body = texts[c.file]
            return (
              <div key={c.id} className="overflow-hidden rounded-lg border border-border bg-background/60">
                <button
                  type="button"
                  onClick={() => setExpanded(isOpen ? null : c.id)}
                  aria-expanded={isOpen}
                  className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition hover:bg-primary/5"
                >
                  <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-baseline gap-x-2">
                      <span className="text-sm font-medium text-card-foreground">{c.name}</span>
                      {c.version && <span className="font-mono text-xs text-muted-foreground">v{c.version}</span>}
                      <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] uppercase text-primary">
                        {c.license}
                      </span>
                    </span>
                    <span className="mt-0.5 block font-mono text-[11px] text-muted-foreground">{c.copyright}</span>
                    <span className="mt-0.5 block text-[11px] text-muted-foreground">{c.scope}</span>
                  </span>
                  <ChevronDown
                    className={cn("h-4 w-4 shrink-0 text-muted-foreground transition", isOpen && "rotate-180")}
                  />
                </button>
                {isOpen && (
                  <pre className="max-h-72 overflow-y-auto border-t border-border bg-card px-3 py-2.5 font-mono text-[11px] leading-5 whitespace-pre-wrap text-muted-foreground">
                    {body ?? "（未取到原文）"}
                  </pre>
                )}
              </div>
            )
          })}

          {components && components.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">载荷里没有组件（不该发生，请报个 issue）。</p>
          )}
        </div>

        <p className="mt-4 shrink-0 border-t border-border pt-3 text-[11px] leading-5 text-muted-foreground">
          本页只列随包分发的许可原文。运行时依赖（Python 包、npm 库）、外部前置组件
          （ffmpeg、VB-CABLE）与模型权重的完整清单及逐条依据，见仓库根目录
          <span className="font-mono"> THIRD_PARTY_NOTICES.md </span>
          —— 它由机器门禁守护，不会过期。
        </p>
      </div>
    </div>
  )
}
