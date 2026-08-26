import { useCallback, useEffect, useRef, useState } from "react"
import { CircleAlert, FileAudio, FolderOutput, Loader2, Sparkles } from "lucide-react"
import {
  exportRvcDataset,
  generateRvcDataset,
  getRvcGenStatus,
  listRvcDataset,
  type RvcDatasetInfo,
} from "@/api/client"

export function RvcDatasetCard() {
  const [info, setInfo] = useState<RvcDatasetInfo | null>(null)
  const [generating, setGenerating] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number; current: string } | null>(null)
  const [exportMsg, setExportMsg] = useState("")
  const [error, setError] = useState("")
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      setInfo(await listRvcDataset())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => () => { if (pollRef.current) window.clearTimeout(pollRef.current) }, [])

  const startGenerate = useCallback(async () => {
    setError("")
    setExportMsg("")
    setGenerating(true)
    try {
      await generateRvcDataset()
      const tick = async () => {
        const st = await getRvcGenStatus()
        setProgress({ done: st.done, total: st.total, current: st.current })
        if (st.running) {
          pollRef.current = window.setTimeout(() => void tick(), 2500)
        } else {
          setGenerating(false)
          setProgress(null)
          if (st.error) setError(st.error)
          else await refresh()
        }
      }
      await tick()
    } catch (e) {
      setGenerating(false)
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [refresh])

  const doExport = useCallback(async () => {
    setError("")
    setExportMsg("")
    try {
      const r = await exportRvcDataset()
      setExportMsg(`已导出 ${r.copied} 个音频到 ${r.dest}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const totalDuration = info?.items.reduce((s, it) => s + it.duration_s, 0) ?? 0

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-primary">RVC TRAIN SET</p>
          <h3 className="mt-2 text-lg font-semibold text-card-foreground">袋鼠 RVC 训练集</h3>
        </div>
        <div className="text-right">
          <p className="font-mono text-xs text-muted-foreground">{info?.items.length ?? 0} 个音频 · {totalDuration.toFixed(0)} 秒</p>
          <p className="mt-1 max-w-[280px] text-right text-[11px] leading-4 text-muted-foreground">目录：{info?.export_dir ?? "media/rvc_dataset"}</p>
        </div>
      </div>

      {generating && progress && (
        <div className="mt-4 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-3 py-2.5 text-xs text-yellow-600">
          <div className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            正在生成语料 {progress.done}/{progress.total}（首次需加载语音模型，约 1~2 分钟）…
          </div>
          {progress.current && <p className="mt-1 truncate">{progress.current}</p>}
        </div>
      )}

      {error && (
        <div className="mt-4 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
          <CircleAlert className="h-4 w-4" />{error}
        </div>
      )}
      {exportMsg && (
        <div className="mt-4 flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2.5 text-xs text-primary">
          <FolderOutput className="h-4 w-4" />{exportMsg}
        </div>
      )}

      <div className="mt-5 flex flex-wrap gap-3">
        <button
          type="button"
          disabled={generating}
          onClick={() => void startGenerate()}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
        >
          {generating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
          {generating ? "正在生成…" : "生成 20 句袋鼠语料"}
        </button>
        <button
          type="button"
          disabled={generating || !info?.items.length}
          onClick={() => void doExport()}
          className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-4 py-2.5 text-sm font-medium text-primary transition hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
        >
          <FolderOutput className="h-4 w-4" />
          导出到 RVC 整合包
        </button>
      </div>

      {info && info.items.length > 0 && (
        <div className="mt-5 max-h-64 overflow-y-auto rounded-lg border border-border bg-background/60">
          <ul className="divide-y divide-border">
            {info.items.map((it) => (
              <li key={it.name} className="flex items-center justify-between gap-2 px-3 py-2 text-xs">
                <span className="inline-flex min-w-0 items-center gap-2 text-card-foreground">
                  <FileAudio className="h-3.5 w-3.5 shrink-0 text-primary" />
                  <span className="truncate font-mono">{it.name}</span>
                </span>
                <span className="shrink-0 font-mono text-muted-foreground">{it.duration_s.toFixed(1)}s · {it.size_kb}KB</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-4 text-xs leading-5 text-muted-foreground">
        生成后到 RVC 整合包（D:\RVC）训练：提取 rmvpe、epoch 200~300、48k 底模，8G 显存全默认即可。
      </p>
    </div>
  )
}
