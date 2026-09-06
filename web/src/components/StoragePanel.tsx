import { useCallback, useEffect, useState } from "react"
import { HardDrive, Loader2, RefreshCw, ShieldCheck, Trash2, X } from "lucide-react"
import { cleanStorage, getStorage } from "@/api/client"
import type { StorageDisk, StorageInfo, StorageItem } from "@/api/client"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

function fmtBytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${n} B`
}

function DiskBar({ d }: { d: StorageDisk }) {
  const pct = Math.min(100, d.used_percent)
  const tone = d.used_percent >= 90 ? "bg-destructive" : d.used_percent >= 75 ? "bg-amber-500" : "bg-primary"
  return (
    <div className="rounded-lg border border-border bg-background/60 p-3">
      <div className="flex items-center justify-between text-xs">
        <span className="font-medium text-card-foreground">{d.drive}（{d.label}）</span>
        <span className={cn("font-mono", d.used_percent >= 90 ? "text-destructive" : "text-muted-foreground")}>
          已用 {d.used_percent}% · 剩 {fmtBytes(d.free_bytes)}
        </span>
      </div>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full transition-all", tone)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function StoragePanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [info, setInfo] = useState<StorageInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [msg, setMsg] = useState("")
  const [checked, setChecked] = useState<Set<string>>(new Set())

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      setInfo(await getStorage())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) { setMsg(""); setChecked(new Set()); void load() }
  }, [open, load])

  if (!open) return null

  const selectedBytes = (info?.items ?? [])
    .filter((i) => checked.has(i.key))
    .reduce((s, i) => s + i.bytes, 0)

  const runClean = async () => {
    if (!checked.size) return
    if (!window.confirm(
      `清理选中的 ${checked.size} 类文件（预计释放 ${fmtBytes(selectedBytes)}）？\n删除后不可恢复（受保护的音色档案与 RVC 权重不受影响）。`,
    )) return
    setBusy(true)
    setError("")
    setMsg("")
    try {
      const r = await cleanStorage([...checked])
      setMsg(
        `已释放 ${fmtBytes(r.freed_bytes)}（${r.removed_files} 个文件）`
        + (r.skipped.length ? `；跳过 ${r.skipped.length} 项` : "")
        + (r.errors.length ? `；${r.errors.length} 个文件删除失败` : ""),
      )
      setChecked(new Set())
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="存储占用">
      <div className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-border bg-card p-5 shadow-2xl sm:p-6">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="font-mono text-xs uppercase tracking-widest text-primary">STORAGE</p>
            <h3 className="mt-1.5 text-lg font-semibold text-card-foreground">存储占用</h3>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              磁盘被缓存悄悄吃满会让训练/推理直接失败——这里统一看、按需清。
              音色档案与 RVC 权重受保护，永远不会被清理。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button type="button" onClick={() => void load()} disabled={loading}
              className="rounded-md p-2 text-muted-foreground transition hover:bg-primary/10 hover:text-primary" aria-label="重新统计">
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
            </button>
            <button type="button" onClick={onClose}
              className="rounded-md p-2 text-muted-foreground transition hover:bg-destructive/10 hover:text-destructive" aria-label="关闭">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {error && <ErrorPanel title="存储信息获取失败" detail={error} className="mt-3" />}
        {msg && (
          <p className="mt-3 rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-xs leading-5 text-primary">{msg}</p>
        )}

        {loading && !info ? (
          <p className="mt-6 flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />正在统计各目录占用…
          </p>
        ) : info ? (
          <>
            <div className="mt-4 space-y-2">
              {info.disks.map((d) => <DiskBar key={d.drive} d={d} />)}
            </div>

            <ul className="mt-4 space-y-2">
              {info.items.map((it: StorageItem) => {
                const isOn = checked.has(it.key)
                return (
                  <li
                    key={it.key}
                    className={cn(
                      "rounded-lg border p-3 transition",
                      isOn ? "border-primary/70 bg-primary/5" : "border-border bg-background/60",
                    )}
                  >
                    <div className="flex items-start gap-3">
                      {it.cleanable ? (
                        <input
                          type="checkbox"
                          checked={isOn}
                          onChange={() => setChecked((cur) => {
                            const next = new Set(cur)
                            if (next.has(it.key)) next.delete(it.key)
                            else next.add(it.key)
                            return next
                          })}
                          className="mt-1 h-3.5 w-3.5 accent-[var(--primary)]"
                          aria-label={`选择清理${it.label}`}
                        />
                      ) : (
                        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                      )}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-baseline justify-between gap-2">
                          <p className="text-xs font-medium text-card-foreground">{it.label}</p>
                          <p className={cn("shrink-0 font-mono text-xs", it.bytes > 0 ? "text-card-foreground" : "text-muted-foreground")}>
                            {fmtBytes(it.bytes)}
                            {it.files > 0 && <span className="ml-1 text-[10px] text-muted-foreground">{it.files} 个文件</span>}
                          </p>
                        </div>
                        <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{it.desc}</p>
                      </div>
                    </div>
                  </li>
                )
              })}
            </ul>

            <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
              <p className="text-xs text-muted-foreground">
                {checked.size ? `已选 ${checked.size} 类 · 约 ${fmtBytes(selectedBytes)}` : "勾选要清理的项（统计为粗略值，以删除结果为准）"}
              </p>
              <button
                type="button"
                disabled={busy || !checked.size}
                onClick={() => void runClean()}
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 disabled:pointer-events-none disabled:opacity-50"
              >
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                {busy ? "清理中…" : "清理选中项"}
              </button>
            </div>
            <p className="mt-3 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <HardDrive className="h-3 w-3" />清理只删文件、不动目录结构；删除在后台逐个执行，被占用的文件会跳过并提示。
            </p>
          </>
        ) : null}
      </div>
    </div>
  )
}
