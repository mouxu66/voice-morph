/**
 * 路由层的兜底：内容区提示 + 加载占位 + 错误边界。
 *
 * 为什么第 4 步才需要
 * ------------------
 * 在这一步之前，`App.tsx` 里 8 个页面都是**静态 import** —— 模块在构建期就绑好了，
 * 不存在「加载中」，也不存在「加载失败」（要失败也是整个 bundle 起不来）。
 * 改成 `React.lazy` 之后页面变成**异步**的，于是多了两种以前不存在的失败：
 *
 *   1. 加载中 —— 需要占位，否则内容区会空白一下；
 *   2. 加载失败（chunk 拿不到 / 页面模块顶层抛异常）—— **没有错误边界的话，
 *      一次失败会让整棵树卸载 → 整个界面白屏**，而用户看到的是"软件坏了"。
 *
 * 这与后端 `plugin_loader` 的原则是同一条：**一个能力坏掉，不能拖垮整个应用。**
 * 后端那边是"一个 router 导不进来只跳过它自己"，前端这边就是下面这几个组件。
 */
import { Component, type ErrorInfo, type ReactNode } from "react"
import { PageShell } from "@/components/layout/PageShell"

/**
 * 内容区的一块提示（标题 + 说明 + 可选细节 + 可选动作）。
 *
 * 抽出来是因为「能力清单拿不到」「一条路由都没有」「这个页面加载失败」三处
 * 说的是同一件事的形状 —— 与其把同一串卡片样式抄三遍，不如只留一份。
 */
export function NoticePanel({
  title,
  desc,
  detail,
  actionLabel,
  onAction,
}: {
  title: string
  desc: string
  /** 给排查用的原始信息（错误消息、缺失路径…）。等宽字体，允许折行 */
  detail?: string
  actionLabel?: string
  onAction?: () => void
}) {
  return (
    <PageShell>
      <div className="rounded-2xl border border-dashed border-border bg-background/50 p-6">
        <h1 className="text-xl font-semibold tracking-tight text-foreground">{title}</h1>
        <p className="mt-1.5 max-w-3xl text-sm leading-6 text-muted-foreground">{desc}</p>
        {detail && <p className="mt-3 break-all font-mono text-xs text-muted-foreground">{detail}</p>}
        {actionLabel && onAction && (
          <button
            type="button"
            onClick={onAction}
            className="mt-4 rounded-lg bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground transition hover:opacity-90"
          >
            {actionLabel}
          </button>
        )}
      </div>
    </PageShell>
  )
}

/** 页面 chunk 加载中的占位。刻意轻：它可能只出现几十毫秒，别闪一个大骨架屏。 */
export function RouteFallback() {
  return (
    <PageShell>
      <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
        正在加载页面…
      </p>
    </PageShell>
  )
}

/**
 * 路由内容区的错误边界。
 *
 * 重试用整页 `location.reload()`，不是"清掉错误状态再渲染一次"：`React.lazy` 会把
 * **失败的 promise 缓存住**，单纯重渲染会立刻拿到同一个失败结果，表现为"点重试没反应"。
 */
export class RouteBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 后端日志就是 stdout；前端这边保持 console，与全站既有的报错口径一致
    console.error("[route] 页面加载失败", error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (error) {
      return (
        <NoticePanel
          title="这个页面没能加载出来"
          desc="其它页面不受影响，可以继续用。多半是页面文件加载失败（打包产物不完整，或页面代码里有报错）。"
          detail={error.message}
          actionLabel="重新加载"
          onAction={() => window.location.reload()}
        />
      )
    }
    return this.props.children
  }
}
