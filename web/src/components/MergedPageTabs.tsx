import { type ReactNode, useEffect } from "react"
import { useSearchParams } from "react-router-dom"
import { cn } from "@/lib/utils"

export type MergedTab = {
  key: string
  label: string
  content: ReactNode
}

/**
 * tab 栏在文档流里占的高度。**同时**用于两处，改一处必须改两处 ——
 * ① 下面那个 `fixed` 栏自身的厚度（内容 + 上下 padding + 1px 边框）；
 * ② 它的占位 div。
 * 以前这里是「`fixed` 栏 + `h-12` 占位」两个各自写死的数字，两者靠肉眼对齐；
 * 现在统一成一个常量，并把值写进 `--tab-bar-h` 给别的浮层让位（见下方 effect）。
 */
const TAB_BAR_H = 50

/** 供「需要避开吸顶栏」的浮层读取；没挂载时不设，变量回退 0 */
const TAB_BAR_VAR = "--tab-bar-h"

/**
 * 合并页 Tab 容器：多个功能合为一页时的顶部切换条。
 * 当前 tab 写进 ?tab= 查询参数（保留深链/刷新），下面渲染对应完整页面组件。
 * tab 栏吸顶在主导航（top-16）之下。
 */
export function MergedPageTabs({ tabs }: { tabs: MergedTab[] }) {
  const [params, setParams] = useSearchParams()
  const current = params.get("tab") ?? tabs[0].key
  const active = tabs.find((t) => t.key === current) ?? tabs[0]

  /**
   * 把吸顶栏的厚度广播出去。
   *
   * 起因（2026-10-03 实测）：导览桌宠 2026-10-03 从「fixed 浮在右上」改成
   * 「文档流顶部横幅」之后，实测仍被这个 `fixed` 栏压住 —— 横幅在文档流里占
   * 56→170px，而这个 fixed 栏固定在 64→104px，**两者不在同一个定位体系**，
   * 于是横幅从它下面穿过去、被盖掉 100%。
   *
   * 之前一直没暴露，是因为页面自己都在这条 fixed 栏**下面**（有 `h-12` 占位），
   * 没有任何东西会主动去占据 64→104px 这段。横幅是第一个这么干的。
   *
   * 所以规矩是：**谁占住吸顶栏，谁就报出它多高**，别让下游靠 `top-*` 魔数互相猜。
   */
  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty(TAB_BAR_VAR, `${TAB_BAR_H}px`)
    // 注意 cleanup 必须返回 void：`removeProperty` 的返回值是 string（被删的旧值），
    // 直接 `return root.style.removeProperty(...)` 会让 TS 判成 `() => string` 而报错。
    return () => {
      root.style.removeProperty(TAB_BAR_VAR)
    }
  }, [])

  return (
    <div>
      <div
        className="fixed inset-x-0 top-16 z-10 border-b border-border bg-background/85 backdrop-blur-xl lg:left-64"
        style={{ height: TAB_BAR_H }}
      >
        <div className="mx-auto flex h-full max-w-7xl items-center gap-1.5 px-5 sm:px-8 lg:px-12">
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
      <div style={{ height: TAB_BAR_H }} aria-hidden="true" />
      {active.content}
    </div>
  )
}
