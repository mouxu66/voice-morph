import { useEffect, useState } from "react"
import { CheckCircle2, FileAudio, FolderCheck, Globe } from "lucide-react"
import { getRvcModel, type RvcModelStatus } from "@/api/client"

export function RvcDatasetCard() {
  const [m, setM] = useState<RvcModelStatus | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    getRvcModel()
      .then((r) => alive && setM(r))
      .catch(() => {})
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  const ready = m?.trained ?? false

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-primary">RVC KANGAROO MODEL</p>
          <h3 className="mt-2 text-lg font-semibold text-card-foreground">袋鼠音色模型</h3>
        </div>
        <div className="text-right">
          {loading ? (
            <span className="text-xs text-muted-foreground">检测中…</span>
          ) : ready ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/15 px-3 py-1 text-xs font-medium text-emerald-500">
              <CheckCircle2 className="h-3.5 w-3.5" /> 已训练完成
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-500/15 px-3 py-1 text-xs font-medium text-amber-500">
              未就绪
            </span>
          )}
        </div>
      </div>

      <ul className="mt-5 space-y-2.5">
        <li className="flex items-center gap-2 text-sm text-card-foreground">
          <FileAudio className="h-4 w-4 text-primary" />
          模型权重 meituan_rat.pth
          <span className={m?.pth_exists ? "ml-auto text-emerald-500" : "ml-auto text-muted-foreground"}>
            {loading ? "…" : m?.pth_exists ? "✓ 就绪" : "缺失"}
          </span>
        </li>
        <li className="flex items-center gap-2 text-sm text-card-foreground">
          <FileAudio className="h-4 w-4 text-primary" />
          特征索引 added_*.index
          <span className={m?.index_exists ? "ml-auto text-emerald-500" : "ml-auto text-muted-foreground"}>
            {loading ? "…" : m?.index_exists ? "✓ 就绪" : "缺失"}
          </span>
        </li>
        <li className="flex items-center gap-2 text-sm text-card-foreground">
          <FolderCheck className="h-4 w-4 text-primary" />
          训练语料
          <span className="ml-auto font-mono text-muted-foreground">{loading ? "…" : `${m?.dataset_count ?? 0} 段`}</span>
        </li>
      </ul>

      <p className="mt-5 flex items-center gap-2 rounded-md border border-primary/20 bg-primary/5 px-3 py-2.5 text-xs leading-5 text-muted-foreground">
        <Globe className="h-4 w-4 shrink-0 text-primary" />
        训练集、检查点与中间产物已收尾归档。实时变声去「袋鼠语音」页一键开启。
      </p>
    </div>
  )
}
