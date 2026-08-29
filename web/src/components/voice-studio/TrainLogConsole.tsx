import { useEffect, useMemo, useRef } from "react"
import { cn } from "@/lib/utils"

/**
 * 训练日志面板：按语义着色 + 自动滚到底，替代原来的灰色控制台观感。
 *
 * 分级规则按 RVC 训练脚本的实际输出标记（[完成]/[失败]/[警告]/阶段名）匹配，
 * 命中不了的按普通信息处理，保证再陌生的日志也不会变成一坨灰。
 */
type TrainLogConsoleProps = {
  lines: string[]
  running: boolean
  className?: string
}

type LineKind = "ok" | "error" | "warn" | "stage" | "info"

const KIND_STYLE: Record<LineKind, string> = {
  ok: "text-emerald-600",
  error: "text-destructive",
  warn: "text-amber-500",
  stage: "text-primary",
  info: "text-muted-foreground",
}

const DOT_COLOR: Record<LineKind, string> = {
  ok: "bg-emerald-500",
  stage: "bg-primary",
  warn: "bg-amber-500",
  error: "bg-destructive",
  info: "bg-muted-foreground",
}

const LEGEND_LABEL: Record<LineKind, string> = {
  ok: "成功",
  stage: "阶段",
  warn: "警告",
  error: "错误",
  info: "信息",
}

function classify(line: string): LineKind {
  if (/\[失败\]|Traceback|Error\b|error:|Exception/i.test(line)) return "error"
  if (/\[完成\]|已完成|成功|done/i.test(line)) return "ok"
  if (/警告|Warning|warn|跳过|已存在/i.test(line)) return "warn"
  if (/^===|阶段|轮次|预处理|F0|HuBERT|索引|提取/i.test(line)) return "stage"
  return "info"
}

export function TrainLogConsole({ lines, running, className }: TrainLogConsoleProps) {
  const boxRef = useRef<HTMLDivElement | null>(null)

  const classified = useMemo(
    () => lines.map((line) => ({ line, kind: classify(line) })),
    [lines],
  )

  // 训练中自动跟随最新日志；停住后不再抢用户的滚动位置
  useEffect(() => {
    if (!running) return
    const box = boxRef.current
    if (box) box.scrollTop = box.scrollHeight
  }, [classified, running])

  return (
    <div className={cn("overflow-hidden rounded-xl border border-border bg-background/70", className)}>
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">训练日志</span>
        <span className="flex items-center gap-1.5">
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              running ? "animate-pulse bg-primary" : "bg-muted-foreground/40",
            )}
          />
          <span className="text-[10px] text-muted-foreground">{running ? "实时输出" : "已停止"}</span>
        </span>
      </div>
      <div ref={boxRef} className="max-h-52 overflow-y-auto px-3 py-2">
        {classified.length === 0 ? (
          <p className="py-4 text-center text-[11px] text-muted-foreground">暂无日志，开始训练后这里会实时滚动。</p>
        ) : (
          <ol className="space-y-0.5">
            {classified.map(({ line, kind }, i) => (
              <li key={`${i}-${line.slice(0, 12)}`} className="flex gap-2 font-mono text-[10px] leading-4">
                <span className="shrink-0 select-none text-muted-foreground/40 tabular-nums">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span className={cn("whitespace-pre-wrap break-all", KIND_STYLE[kind])}>{line}</span>
              </li>
            ))}
          </ol>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border px-3 py-1.5">
        {(["ok", "stage", "warn", "error"] as LineKind[]).map((k) => (
          <span key={k} className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <span className={cn("h-1.5 w-1.5 rounded-full", DOT_COLOR[k])} />
            {LEGEND_LABEL[k]}
          </span>
        ))}
      </div>
    </div>
  )
}
