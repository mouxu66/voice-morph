import { CheckCircle2, Sparkles, Target } from "lucide-react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"

/**
 * 效果阶梯：告诉用户「素材量 → 能做什么」的递进关系，
 * 并用"你现在在这里"标注当前位置，驱动"继续采集/训练"的动机。
 * 组件不取数，由调用方算好 currentLevel 传入，做到处处可复用。
 */

export type LadderTier = {
  /** 0 / 1 / 2：档位序号（0 为空态起点，1=秒变声，2=实时，3=越练越像） */
  level: number
  /** 平民化档位名 */
  name: string
  /** 素材目标（大白话） */
  target: string
  /** 动机一句：为什么值得做到这一档 */
  why: string
  /** 通往该档能力的入口（路由 + 可选 tab） */
  to: string
  cta: string
}

export const EFFECT_LADDER_TIERS: LadderTier[] = [
  {
    level: 1,
    name: "秒变声",
    target: "只要有 5~10 秒干净人声",
    why: "选个音色『输字变声』，马上在微信里听到你的声音说话——先尝尝是不是这个味。",
    to: "/tts?tab=single",
    cta: "去输字变声",
  },
  {
    level: 2,
    name: "实时变声",
    target: "再攒到约 1 分钟素材",
    why: "照着你的腔调专门练一副嗓子，开麦就能用——打游戏、开会、微信语音都行。",
    to: "/workshop",
    cta: "去攒素材",
  },
  {
    level: 3,
    name: "越练越像",
    target: "攒到 5 分钟以上",
    why: "口型、语气、情绪都跟得上，最难分辨。素材多是质变的关键，值得坚持。",
    to: "/workshop?tab=ft",
    cta: "去精修音色",
  },
]

export function EffectLadderCard({
  currentLevel = 0,
  collectedSeconds,
  modelReady,
  compact = false,
}: {
  /** 0=还没有任何音色素材；1=秒变声可用；2=实时变声可用；3=长素材精修阶段 */
  currentLevel?: number
  /** 已采集素材秒数（可空，显示在顶部摘要） */
  collectedSeconds?: number
  /** 是否已有训练好的模型（实时变声可用） */
  modelReady?: boolean
  /** 紧凑版：只显示横向阶梯，不放大段说明（给页面局部嵌入用） */
  compact?: boolean
}) {
  const level = Math.min(3, Math.max(0, currentLevel))
  const reached = level >= 1 ? "已能用『秒变声』（输字就变，免训练）" : "还没有可用音色"
  const next = EFFECT_LADDER_TIERS.find((t) => t.level === Math.min(3, level + 1))

  return (
    <div className="overflow-hidden rounded-2xl border border-border bg-card/85 shadow-md">
      {/* 头部：一句话点题 + 当前位置 */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-primary/5 px-5 py-4">
        <p className="flex items-center gap-2 text-sm font-semibold text-card-foreground">
          <Sparkles className="h-4 w-4 text-primary" />
          素材越多，声音越像
        </p>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2.5 py-1 text-xs text-primary">
          <Target className="h-3.5 w-3.5" />
          你现在：{modelReady ? "可实时变声" : level === 0 ? "刚开始" : level === 1 ? "可秒变声" : level === 2 ? "可实时变声" : "高阶精修"}
        </span>
      </div>

      <div className={cn("space-y-3", compact ? "p-4" : "p-5")}>
        {/* 顶部摘要（非紧凑） */}
        {!compact && (
          <p className={cn("rounded-xl border px-4 py-3 text-xs leading-5", level > 0 ? "border-emerald-500/40 bg-emerald-500/5 text-emerald-600" : "border-border bg-background/60 text-muted-foreground")}>
            <CheckCircle2 className="mr-1.5 inline h-3.5 w-3.5" />
            {reached}
            {collectedSeconds != null && collectedSeconds > 0 && (
              <span className="ml-1 font-mono opacity-80">（已攒 {Math.round(collectedSeconds)} 秒）</span>
            )}
          </p>
        )}

        {/* 竖向三段阶梯轨 + 档位块 */}
        <div className="flex flex-col gap-1">
          {EFFECT_LADDER_TIERS.map((t, idx) => {
            const done = level > idx
            const active = t.level === level && level > 0
            return (
              <Link
                key={t.level}
                to={t.to}
                onClick={(e) => { if (level < t.level - 1) e.preventDefault() }}
                className={cn(
                  "group flex items-stretch gap-3 rounded-xl border p-3.5 transition hover:shadow-md",
                  active || done
                    ? "border-primary/40 bg-gradient-to-br from-primary/10 to-card"
                    : "border-border bg-background/40 hover:border-primary/30",
                )}
              >
                {/* 步骤轨道 */}
                <div className="flex flex-col items-center">
                  <span className={cn("flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold", done ? "bg-primary text-primary-foreground" : active ? "bg-primary/15 text-primary" : "bg-muted text-muted-foreground")}>
                    {done ? <CheckCircle2 className="h-4 w-4" /> : t.level}
                  </span>
                  {idx < EFFECT_LADDER_TIERS.length - 1 && <span className={cn("mt-1 w-px flex-1", done ? "bg-primary/40" : "bg-border")} />}
                </div>

                {/* 档位内容 */}
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm font-semibold text-card-foreground">{t.name}</p>
                    {active && (
                      <span className="rounded-full bg-primary px-2 py-0.5 text-[10px] font-medium text-primary-foreground">你在这里</span>
                    )}
                    {done && <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-medium text-emerald-600">已达成</span>}
                  </div>
                  <p className="mt-0.5 text-[11px] font-medium text-primary">{t.target}</p>
                  {!compact && <p className="mt-1.5 text-xs leading-5 text-muted-foreground">{t.why}</p>}
                  {!compact && (
                    <span className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary opacity-0 transition group-hover:opacity-100">
                      {t.cta}
                    </span>
                  )}
                </div>
              </Link>
            )
          })}
        </div>

        {/* 下一步建议（非紧凑且有下一步时显示） */}
        {!compact && next && level < 3 && (
          <p className="flex items-start gap-2 rounded-xl border border-primary/30 bg-primary/5 px-4 py-3 text-xs leading-5 text-muted-foreground">
            <Target className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
            下一步：{next.cta}，{next.target}。{next.why}
          </p>
        )}
      </div>
    </div>
  )
}