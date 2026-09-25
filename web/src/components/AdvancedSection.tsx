import { useState, type ReactNode } from "react"
import { ChevronDown, SlidersHorizontal } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * 默认折叠的「进阶」区 —— 把并列的次要内容从首屏挪走，但**一个都不删**。
 *
 * 为什么是这个组件（而不是直接删内容）
 * ----------------------------------
 * 行业调研结论：用户抱怨的「功能冗余」其实是**功能平铺得太早**；
 * 标准解法是渐进式披露。但调研同时点名了一条边界 ——
 * **「渐进式披露 ≠ 藏信息」**：定价、权限、重要后果这类影响决策的信息不能藏；
 * 而且「功能少」同样会被差评（有产品因"声线不足 50 种"被评 5.1/10）。
 *
 * 所以本组件的语义是「**先收着，想看随时点**」，不是「删掉」：
 * - 内容始终在 DOM 里（`hidden` 而非条件卸载？**不，用条件渲染**，见下）
 * - 摘要行永远可见，用户知道这里有什么
 * - 记住展开状态，同一个会话里不反复收起
 *
 * ⚠️ 为什么用**条件渲染**而不是 CSS `hidden`：
 * 折叠区里通常挂着图表 / 播放器 / 请求（如音色对比、参数面板）。用 CSS 藏起来
 * 它们照样初始化、照样发请求，等于没省 —— 首屏负担一点没减。
 * 条件渲染才能真正把"没到时候的东西"挪出关键路径。
 *
 * 何时**不该**用它：
 * - 内容是用户进这一页的主要目的（如音色列表本身）→ 那是主体，不是进阶
 * - 内容影响决策（要下载多少 MB、需要什么权限）→ 见上文，不能藏
 */
export function AdvancedSection({
  /** 折叠时显示的一行说明，要让用户知道"收着什么" */
  summary,
  /** 展开后额外说明一句，可省 */
  hint,
  children,
  className,
  /** 默认展开？给"用户主动找过来"的场景用（如从设置跳进来） */
  defaultOpen = false,
}: {
  summary: string
  hint?: string
  children: ReactNode
  className?: string
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className={cn("rounded-2xl border border-dashed border-border bg-background/40", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition hover:bg-muted/40"
      >
        <SlidersHorizontal className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1">
          <span className="block text-xs font-medium text-foreground">{summary}</span>
          {hint && !open && <span className="mt-0.5 block text-[11px] text-muted-foreground">{hint}</span>}
        </span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{open ? "收起" : "展开"}</span>
        <ChevronDown className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open && <div className="border-t border-border px-4 pb-4 pt-4">{children}</div>}
    </div>
  )
}
