import { Link } from "react-router-dom"
import { Shirt } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * 「去试衣间试穿」入口 —— 摆在各个单项工具页里。
 *
 * 为什么需要：实时变声 / 离线变声 / 输字变声各自只解决一件事，但用户想「同一段声音
 * 一次试好几个音色」时，站内没有路标，只能一个个页面手动来回试。这个小卡片就是那条
 * 路标，指向统一的编排页；引擎仍是各页自己的，不重复实现。
 */
export function FittingEntry({
  hint = "想拿同一段声音一次试好几个音色？",
  className,
  variant = "card",
}: {
  hint?: string
  className?: string
  /** card = 侧栏里的独立小块；inline = 挤在密集排版里的一行 */
  variant?: "card" | "inline"
}) {
  if (variant === "inline") {
    return (
      <span className={cn("inline-flex items-center gap-1.5 text-xs text-muted-foreground", className)}>
        <Shirt className="h-3.5 w-3.5 shrink-0 text-primary" />
        {hint}
        <Link to="/fitting" className="font-medium text-primary underline underline-offset-2">
          去试衣间 →
        </Link>
      </span>
    )
  }
  return (
    <div
      className={cn(
        "rounded-2xl border border-primary/30 bg-primary/[0.07] p-4 shadow-lg sm:p-5",
        className,
      )}
    >
      <p className="flex items-center gap-2 text-xs font-medium text-primary">
        <Shirt className="h-3.5 w-3.5" />试衣间
      </p>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">
        {hint}把音色当衣服，一次挂一排，同一段声音并排试穿，还能对着麦克风点哪件换哪件。
      </p>
      <Link
        to="/fitting"
        className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      >
        <Shirt className="h-3.5 w-3.5" />去试衣间试穿
      </Link>
    </div>
  )
}
