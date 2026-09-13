import { useEffect, useState } from "react"
import { CheckCircle2, FileAudio, FolderCheck, Globe, TriangleAlert } from "lucide-react"
import { getRvcModel, type RvcModelStatus } from "@/api/client"

export function RvcDatasetCard() {
  const [m, setM] = useState<RvcModelStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    getRvcModel()
      .then((r) => {
        if (!alive) return
        setM(r)
        setFailed(false)
      })
      // "后端没响应"与"真没训练"必须分开：同理（catch(() => {})）会让两种状态显示成
      // 同一句"未就绪"，用户会去重训一个其实已经训好的模型（2026-09-13 修复）。
      .catch(() => {
        if (alive) setFailed(true)
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const ready = m?.trained ?? false
  const exp = m?.exp ?? ""
  // 权重文件名 = 后端返回的实验名，不再写死某个音色（那对别人机器是不存在的）
  const weightsName = exp ? `${exp}.pth` : "未选择音色"
  const fileState = (ok?: boolean) => (loading ? "…" : failed ? "未知" : ok ? "✓ 就绪" : "缺失")

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-primary">RVC MODEL</p>
          <h3 className="mt-2 text-lg font-semibold text-card-foreground">实时变声模型</h3>
        </div>
        <div className="text-right">
          {loading ? (
            <span className="text-xs text-muted-foreground">检测中…</span>
          ) : failed ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-destructive/15 px-3 py-1 text-xs font-medium text-destructive">
              <TriangleAlert className="h-3.5 w-3.5" /> 检测失败
            </span>
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
          模型权重 {weightsName}
          <span className={m?.pth_exists ? "ml-auto text-emerald-500" : "ml-auto text-muted-foreground"}>
            {fileState(m?.pth_exists)}
          </span>
        </li>
        <li className="flex items-center gap-2 text-sm text-card-foreground">
          <FileAudio className="h-4 w-4 text-primary" />
          特征索引 added_*.index
          <span className={m?.index_exists ? "ml-auto text-emerald-500" : "ml-auto text-muted-foreground"}>
            {fileState(m?.index_exists)}
          </span>
        </li>
        <li className="flex items-center gap-2 text-sm text-card-foreground">
          <FolderCheck className="h-4 w-4 text-primary" />
          训练语料
          <span className="ml-auto font-mono text-muted-foreground">
            {loading || failed ? "…" : `${m?.dataset_count ?? 0} 段`}
          </span>
        </li>
      </ul>

      {failed ? (
        <p className="mt-5 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs leading-5 text-destructive">
          <TriangleAlert className="h-4 w-4 shrink-0" />
          后端未响应，暂时无法判断模型状态 —— 这不代表未训练。确认后端已启动后重试。
        </p>
      ) : !loading && !exp ? (
        <p className="mt-5 flex items-center gap-2 rounded-md border border-primary/20 bg-primary/5 px-3 py-2.5 text-xs leading-5 text-muted-foreground">
          <Globe className="h-4 w-4 shrink-0 text-primary" />
          还没有默认音色：先到「音色库」创建并训练一个，或在 .env 里用 VM_RVC_EXP 指定。
        </p>
      ) : (
        <p className="mt-5 flex items-center gap-2 rounded-md border border-primary/20 bg-primary/5 px-3 py-2.5 text-xs leading-5 text-muted-foreground">
          <Globe className="h-4 w-4 shrink-0 text-primary" />
          训练集、检查点与中间产物已收尾归档。实时变声去「实时变声」页一键开启。
        </p>
      )}
    </div>
  )
}
