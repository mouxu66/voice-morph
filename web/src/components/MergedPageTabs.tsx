import type { ReactNode } from "react"
import { useSearchParams } from "react-router-dom"
import { cn } from "@/lib/utils"

export type MergedTab = {
  key: string
  label: string
  content: ReactNode
}

/**
 * 合并页 Tab 容器：多个功能合为一页时的顶部切换条。
 * 当前 tab 写进 ?tab= 查询参数（保留深链/刷新），下面渲染对应完整页面组件。
 * tab 栏吸顶在主导航（top-16）之下。
 */
export function MergedPageTabs({ tabs }: { tabs: MergedTab[] }) {
  const [params, setParams] = useSearchParams()
  const current = params.get("tab") ?? tabs[0].key
  const active = tabs.find((t) => t.key === current) ?? tabs[0]

  return (
    <div>
      <div className="fixed inset-x-0 top-16 z-10 border-b border-border bg-background/85 backdrop-blur-xl lg:left-64">
        <div className="mx-auto flex max-w-7xl gap-1.5 px-5 py-2 sm:px-8 lg:px-12">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setParams({ tab: t.key })}
              className={cn(
                "inline-flex shrink-0 items-center gap-2 rounded-md px-3.5 py-2 text-xs transition",
                t.key === active.key
                  ? "bg-primary text-primary-foreground shadow-md"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {/* 占位：tab 栏为 fixed，避免遮住各子页面自己的 header */}
      <div className="h-12" aria-hidden="true" />
      {active.content}
    </div>
  )
}
