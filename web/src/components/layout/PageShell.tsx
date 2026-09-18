import type { ElementType, ReactNode } from "react"
import { cn } from "@/lib/utils"

/**
 * PageShell —— 全站唯一的页面内容容器。
 *
 * 背景：此前每个页面各自写内边距（`px-5 py-10 sm:px-8 lg:px-12 lg:py-14` 这一串
 * 在三个页面里逐字重复），且都用 `max-w-7xl`（1280）。结果是窄窗口内容贴边、
 * 宽窗口又因留白不足而显得拥挤，切换页面时内容基线还会跳动。
 * 现在宽度与留白只在这里定义一处。
 */
export function PageShell({
  children,
  className,
  wide = false,
  padded = true,
}: {
  children: ReactNode
  className?: string
  /** 表格 / 画廊这类需要更多横向空间的页面用 wide */
  wide?: boolean
  /** 自带纵向节奏的区块（如首屏 Hero）可关掉默认内边距，只沿用宽度与横向留白 */
  padded?: boolean
}) {
  return (
    <div
      className={cn(
        "mx-auto w-full",
        padded && "px-6 pb-20 pt-8 lg:px-10 lg:pt-10",
        !padded && "px-6 lg:px-10",
        wide ? "max-w-[1520px]" : "max-w-[1240px]",
        className,
      )}
    >
      {children}
    </div>
  )
}

/**
 * Section —— 页面内的一个语义区块。
 *
 * 替代原先「font-mono 大写英文小标 + text-2xl 标题 + 说明」的三行头。
 * 那个写法在首页出现了七次、在全站各页重复，等重堆叠没有主次，且英文标签
 * 对中文用户零信息量。这里改为：可选的中文序号短标（弱）+ 20px 中文标题 + 说明，
 * 并把右侧动作区收进同一行，避免标题独占一行后右侧空出一大片。
 */
export function Section({
  id,
  eyebrow,
  title,
  desc,
  actions,
  children,
  className,
  bodyClassName,
  as: Tag = "section",
}: {
  id?: string
  eyebrow?: ReactNode
  title?: ReactNode
  desc?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  bodyClassName?: string
  as?: ElementType
}) {
  const hasHead = Boolean(eyebrow || title || desc || actions)
  return (
    <Tag id={id} className={cn("scroll-mt-24", className)}>
      {hasHead && (
        <div className="mb-4 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            {eyebrow && (
              <p className="mb-1.5 flex items-center gap-2 text-xs font-medium text-primary">
                <span className="h-3 w-0.5 rounded-full bg-primary/70" aria-hidden="true" />
                {eyebrow}
              </p>
            )}
            {title && <h2 className="text-xl font-semibold tracking-tight text-foreground">{title}</h2>}
            {desc && <p className="mt-1.5 max-w-3xl text-sm leading-6 text-muted-foreground">{desc}</p>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      {children && <div className={bodyClassName}>{children}</div>}
    </Tag>
  )
}

/**
 * Card —— 统一的表面容器。收拢全站 `rounded-2xl border border-border bg-card/85
 * shadow-md` 这一串重复，并给出三档层次：flat（贴在页面背景上）/ raised（默认）/ hero（强调）。
 */
export function Card({
  as: Tag = "div",
  tone = "raised",
  interactive = false,
  className,
  children,
  onClick,
  title,
}: {
  as?: ElementType
  tone?: "flat" | "raised" | "accent"
  interactive?: boolean
  className?: string
  children?: ReactNode
  onClick?: () => void
  title?: string
}) {
  return (
    <Tag
      onClick={onClick}
      title={title}
      className={cn(
        "rounded-2xl border",
        tone === "flat" && "border-border/70 bg-background/40",
        tone === "raised" && "border-border bg-card/80 shadow-md backdrop-blur-xl",
        tone === "accent" && "border-primary/35 bg-primary/[0.07]",
        interactive && "transition duration-200 hover:border-input hover:shadow-lg",
        className,
      )}
    >
      {children}
    </Tag>
  )
}
