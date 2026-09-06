import { useEffect, useRef, useState } from "react"
import { AlertTriangle, Check, Copy } from "lucide-react"
import { notify } from "@/lib/notify"

/**
 * 统一报错卡片：像普通网页一样把「哪里出错 + 完整错误原文」摊开，
 * 并附一键复制，方便用户直接把报错拿去问自己的 AI 求根因。
 *
 * - title：发生在哪一步 / 哪个功能（告诉用户「哪里」有问题）
 * - detail：完整的错误原文（不截断，可滚动、可选中）
 * - hint：可选的排查建议
 */
export function ErrorPanel({
  title, detail, hint,   className = "",
}: {
  title: string
  detail?: string | null
  hint?: string | null
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  // 错误出现（或内容更新）时同步弹一次全局 toast，让「埋在表单里的内联报错」也显眼可见。
  // 用 ref 记录已提醒的内容，避免同一错误反复挂载时重复弹窗。
  const firedRef = useRef<string | null>(null)
  useEffect(() => {
    const key = `${title}::${detail ?? ""}`
    if (firedRef.current === key) return
    firedRef.current = key
    notify.error(title, detail ?? undefined)
  }, [title, detail])
  const text = [title, detail, hint ? `排查建议：${hint}` : null]
    .filter(Boolean)
    .join("\n")
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // Electron file:// 页面不在 secure context，navigator.clipboard 不可用；
      // 退回 execCommand（隐藏 textarea 选中复制），仍失败则用户可手动选中文本
      const ta = document.createElement("textarea")
      ta.value = text
      ta.style.position = "fixed"
      ta.style.opacity = "0"
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand("copy") } catch { /* 放弃，按钮静默 */ }
      ta.remove()
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div
      className={`flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-sm text-destructive ${className}`}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        {detail ? (
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-5 text-foreground/90">
            {detail}
          </pre>
        ) : null}
        {hint ? <p className="mt-1.5 text-xs text-muted-foreground">💡 {hint}</p> : null}
      </div>
      <button
        type="button"
        onClick={copy}
        title="复制完整报错，可粘贴给 AI 分析根因"
        className="shrink-0 rounded-md border border-destructive/30 bg-background/40 px-2 py-1 text-destructive transition hover:bg-destructive/10"
      >
        {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </div>
  )
}
