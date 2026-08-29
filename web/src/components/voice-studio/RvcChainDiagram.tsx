import { ArrowRight, AudioWaveform, Cable, CircleAlert, Headphones, MessageSquare, Mic, Volume2 } from "lucide-react"
import { cn } from "@/lib/utils"

/**
 * 声音去哪儿了？—— 实时变声的设备链路图。
 *
 * 麦克风 → RVC 变声 → CABLE Input →(虚拟跳线)→ CABLE Output → 微信/游戏
 *
 * 图上每个节点按真实状态着色（就绪/运行中/未就绪），
 * 让用户不用猜"声音从哪进、从哪出、为什么对方听不到"。
 */
type RvcChainDiagramProps = {
  inputDevice: string
  outputDevice: string
  liveRunning: boolean
  modelReady: boolean
  audioSwitched: boolean
  exp: string
  className?: string
}

type NodeState = "active" | "ready" | "idle" | "blocked"

function ChainNode({
  icon: Icon,
  title,
  sub,
  state,
  badge,
}: {
  icon: typeof Mic
  title: string
  sub: string
  state: NodeState
  badge?: string
}) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-1 flex-col items-center gap-1.5 rounded-xl border px-3 py-3 text-center transition",
        state === "active" && "border-primary bg-primary/10 shadow-[0_0_0_1px_rgb(var(--c-accent)/0.25)]",
        state === "ready" && "border-border bg-card",
        state === "idle" && "border-border bg-card/50",
        state === "blocked" && "border-destructive/40 bg-destructive/10",
      )}
    >
      <span
        className={cn(
          "flex h-9 w-9 items-center justify-center rounded-lg",
          state === "active" && "bg-primary text-primary-foreground",
          state === "ready" && "bg-muted text-foreground",
          state === "idle" && "bg-muted text-muted-foreground",
          state === "blocked" && "bg-destructive/15 text-destructive",
        )}
      >
        <Icon className="h-4 w-4" />
      </span>
      <p className={cn("truncate text-xs font-medium", state === "idle" ? "text-muted-foreground" : "text-foreground")}>
        {title}
      </p>
      <p className="line-clamp-2 text-[10px] leading-4 text-muted-foreground">{sub}</p>
      {badge && (
        <span
          className={cn(
            "rounded-full px-1.5 py-0.5 text-[9px] font-medium",
            state === "active" && "bg-primary/20 text-primary",
            state === "ready" && "bg-muted text-muted-foreground",
            state === "idle" && "bg-muted text-muted-foreground",
            state === "blocked" && "bg-destructive/20 text-destructive",
          )}
        >
          {badge}
        </span>
      )}
    </div>
  )
}

function Arrow() {
  return <ArrowRight className="hidden h-4 w-4 shrink-0 text-muted-foreground/60 sm:block" aria-hidden="true" />
}

export function RvcChainDiagram({
  inputDevice,
  outputDevice,
  liveRunning,
  modelReady,
  audioSwitched,
  exp,
  className,
}: RvcChainDiagramProps) {
  const modelState: NodeState = liveRunning ? "active" : modelReady ? "ready" : "blocked"

  return (
    <div className={cn("rounded-xl border border-border bg-background/60 p-4", className)}>
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs font-medium text-foreground">设备链路</p>
        <span
          className={cn(
            "rounded-full px-2 py-0.5 text-[10px] font-medium",
            liveRunning ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground",
          )}
        >
          {liveRunning ? "链路已打通" : "链路未启动"}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2 sm:flex sm:items-stretch sm:gap-1.5">
        <ChainNode
          icon={Mic}
          title="麦克风"
          sub={inputDevice || "默认输入"}
          state={liveRunning ? "active" : "ready"}
          badge="输入"
        />
        <Arrow />
        <ChainNode
          icon={AudioWaveform}
          title="RVC 变声"
          sub={exp || "未选音色"}
          state={modelState}
          badge={modelReady ? "模型就绪" : "未训练"}
        />
        <Arrow />
        <ChainNode
          icon={Cable}
          title="CABLE Input"
          sub={outputDevice || "虚拟声卡"}
          state={liveRunning ? "active" : "ready"}
          badge="写入"
        />
        <Arrow />
        <ChainNode
          icon={Headphones}
          title="CABLE Output"
          sub="系统默认麦克风"
          state={audioSwitched ? "active" : "idle"}
          badge={audioSwitched ? "已切换" : "未切换"}
        />
        <Arrow />
        <ChainNode
          icon={MessageSquare}
          title="微信 / 游戏"
          sub="对方听到的声音"
          state={liveRunning ? "active" : "idle"}
          badge="接收"
        />
      </div>

      {!modelReady && (
        <p className="mt-3 flex items-start gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-1.5 text-[11px] leading-4 text-destructive">
          <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          该音色还没有训练好的 RVC 模型，链路在第二步就断了 —— 先在右侧训练。
        </p>
      )}
      <p className="mt-3 flex items-start gap-1.5 text-[11px] leading-4 text-muted-foreground">
        <Volume2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
        扬声器仍是你的真实音箱，所以你自己听到的是原声、对方听到的是变声，这是正常的。已打开的微信/游戏需退出重开才会改用新麦克风。
      </p>
    </div>
  )
}
