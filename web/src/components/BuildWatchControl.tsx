import { Play, Square, Loader2, CheckCircle2 } from "lucide-react"
import { cn } from "@/lib/utils"
import { useBuildWatch } from "@/hooks/useBuildWatch"

/**
 * 构建监听器控制面板
 * 
 * 显示当前状态并提供启动/停止按钮
 */
export function BuildWatchControl() {
  const { status, start, stop, loading } = useBuildWatch()
  
  if (!status) {
    // 加载状态
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span>检查中...</span>
      </div>
    )
  }
  
  if (!status.hasScript) {
    // 脚本不存在
    return (
      <div className="text-xs text-destructive">
        未找到构建监听脚本
      </div>
    )
  }
  
  const isRunning = status.isRunning
  
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-2">
        {isRunning ? (
          <>
            <CheckCircle2 className="h-4 w-4 text-emerald-500" />
            <span className="text-sm font-medium text-foreground">监听中</span>
          </>
        ) : (
          <>
            <Square className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm text-muted-foreground">已停止</span>
        </>
        )}
      </div>
      
      <button
        type="button"
        onClick={isRunning ? stop : start}
        disabled={loading}
        className={cn(
          "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition",
          isRunning
            ? "bg-destructive/10 text-destructive hover:bg-destructive/20"
            : "bg-primary text-primary-foreground hover:bg-primary/90",
          loading && "opacity-60 cursor-not-allowed",
        )}
      >
        {loading ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : isRunning ? (
          <Square className="h-3 w-3" />
        ) : (
          <Play className="h-3 w-3" />
        )}
        {isRunning ? "停止" : "启动"}
      </button>
    </div>
  )
}
