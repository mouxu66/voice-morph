import { CheckCircle2, CircleAlert, Loader2, RefreshCw } from "lucide-react"
import type { DiagnoseItem } from "@/types"
import { cn } from "@/lib/utils"

/**
 * 链路自检结果列表 —— 实时变声（SEND CHAIN）与输字变声共用的渲染件。
 *
 * 后端 /audio/send_chain 与 /tts/send_chain 的 items 刻意同构
 * （key/ok/warn/label/detail/hint），所以渲染只写一份：
 * 两边差别在「检查什么」与「怎么修」，那属于数据与 onFix 回调，不属于渲染。
 *
 * allOkText 由调用方给：两条链路的"全绿"含义不同，不能共用一句话。
 */
export function ChainResultList({
  items,
  allOkText,
  allOk = null,
  extraIssue = null,
  fixingKey = null,
  onFix,
}: {
  items: DiagnoseItem[]
  /** 全绿时的一行说明（两条链路语义不同，必须由调用方给） */
  allOkText: string
  /** 后端给的整体结论；传 null 表示只看 items（如叠加了前端本地检查） */
  allOk?: boolean | null
  /** 前端本地追加的一条问题（如"没检测到麦克风"），不属于后端 items */
  extraIssue?: { label: string; detail: string; hint: string } | null
  fixingKey?: string | null
  /** 哪些 key 提供一键修复；不传则该链路只展示不修复 */
  onFix?: (key: string) => void
}) {
  const issues = items.filter((it) => !it.ok)
  const ok = allOk === null ? issues.length === 0 && !extraIssue : allOk && !extraIssue

  if (ok) {
    return (
      <p className="mt-4 flex items-start gap-2 text-xs text-emerald-600">
        <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{allOkText}</span>
      </p>
    )
  }

  return (
    <ul className="mt-4 space-y-2">
      {issues.map((it) => (
        <li
          key={it.key}
          className={cn(
            "flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2.5 text-xs",
            it.warn
              ? "border-amber-500/40 bg-amber-500/10"
              : "border-destructive/40 bg-destructive/10",
          )}
        >
          <CircleAlert className={cn("h-3.5 w-3.5 shrink-0", it.warn ? "text-amber-500" : "text-destructive")} />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-card-foreground">{it.label}：</span>
            {it.detail}
            {it.hint && <span className="mt-0.5 block leading-4 text-muted-foreground">{it.hint}</span>}
          </span>
          {onFix && FIXABLE.has(it.key) && (
            <button
              type="button"
              onClick={() => onFix(it.key)}
              disabled={fixingKey !== null}
              className="inline-flex shrink-0 items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 font-medium text-primary transition hover:bg-primary/20 disabled:pointer-events-none disabled:opacity-50"
            >
              {fixingKey === it.key ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <RefreshCw className="h-3 w-3" />
              )}
              {fixingKey === it.key ? "修复中…" : fixLabel(it.key)}
            </button>
          )}
        </li>
      ))}
      {extraIssue && (
        <li className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs">
          <CircleAlert className="h-3.5 w-3.5 shrink-0 text-amber-500" />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-card-foreground">{extraIssue.label}：</span>
            {extraIssue.detail}
            <span className="mt-0.5 block leading-4 text-muted-foreground">{extraIssue.hint}</span>
          </span>
        </li>
      )}
    </ul>
  )
}

/** 提供一键修复的 key 白名单 —— 与后端 hint 保持一致，不做"点了没反应"的按钮 */
const FIXABLE = new Set(["default_capture", "stale_backup"])

function fixLabel(key: string): string {
  return key === "default_capture" ? "一键最优" : "恢复默认设备"
}
