import { CheckCircle2, CircleAlert, Loader2, Mic2, Play, RefreshCw, Repeat2, Square } from "lucide-react"
import type { ReactNode } from "react"
import type { useLive } from "@/pages/Live/useLive"

function StatusBadge({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600" : "border-muted bg-muted/40 text-muted-foreground"}`}>
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <CircleAlert className="h-3.5 w-3.5" />}
      {children}
    </span>
  )
}

function TrainButton({ p, label }: { p: ReturnType<typeof useLive>; label: string }) {
  return (
    <button
      type="button"
      onClick={p.train}
      disabled={p.training}
      className="inline-flex w-full items-center justify-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-4 py-3 text-sm font-medium text-primary transition hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
    >
      {p.training ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic2 className="h-4 w-4" />}
      {p.training ? "正在启动…" : label}
    </button>
  )
}

export function LivePage(p: ReturnType<typeof useLive>) {
  const disabled = !p.modelOk && !p.liveOn
  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">REALTIME / VOICE CHANGER</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">实时变声</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            一键启动 RVC 实时变声：自动切换虚拟声卡，对着麦克风说话即是目标音色；停止后自动还原设备设置。
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <StatusBadge ok={p.modelOk}>RVC 模型{p.modelOk ? "已就绪" : "未就绪"}</StatusBadge>
            <StatusBadge ok={p.datasetCount > 0}>{p.datasetCount} 条语料</StatusBadge>
            <StatusBadge ok={p.liveOn}>实时{p.liveOn ? "运行中" : "未启动"}</StatusBadge>
            <StatusBadge ok={p.trainingRunning}>训练{p.trainingRunning ? "进行中" : "空闲"}</StatusBadge>
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-2 lg:px-12 lg:py-14">
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">01 / 变声</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <Repeat2 className="h-4 w-4 text-primary" />实时变声开关
          </h3>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            一键启动：自动把录音设备切到虚拟声卡（{p.liveStatus?.output_device ?? "CABLE Output"}），退出或停止后自动还原你的设备设置。
          </p>
          {p.liveOn ? (
            <button
              type="button"
              onClick={p.stop}
              disabled={p.stopping}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-md bg-destructive px-4 py-3 text-sm font-medium text-destructive-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-60"
            >
              {p.stopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              {p.stopping ? "正在停止并还原声卡…" : "停止实时变声"}
            </button>
          ) : (
            <button
              type="button"
              onClick={p.start}
              disabled={p.starting || disabled}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
            >
              {p.starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {p.starting ? "正在启动…" : "开始实时变声"}
            </button>
          )}
          {p.liveOn && (
            <p className="mt-3 flex items-center gap-2 text-xs text-primary">
              <CircleAlert className="h-3.5 w-3.5 shrink-0" />实时变声中：关闭 RVC 窗口或点「停止」会自动还原设备；若忘了关，随时在左侧栏点「一键恢复音频」。
            </p>
          )}
          {!p.liveOn && p.liveStatus?.last_error && (
            <p className="mt-3 flex items-center gap-2 text-xs text-destructive">
              <CircleAlert className="h-3.5 w-3.5 shrink-0" />上次还原声卡失败：{p.liveStatus.last_error}。请点「一键恢复音频」重试。
            </p>
          )}
          {!p.modelOk && (
            <p className="mt-3 flex items-center gap-2 text-xs text-destructive">
              <CircleAlert className="h-3.5 w-3.5" />RVC 模型未就绪，请先点「训练模型」。
            </p>
          )}
        </section>

        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">02 / 训练</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <Mic2 className="h-4 w-4 text-primary" />训练 RVC 模型
          </h3>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            自动跑完预处理 → 提音高 → 提特征 → 训练 → 生成索引，下方实时显示进度；模型就绪后即可实时变声。
          </p>

          {p.trainingRunning ? (
            <div className="mt-5 space-y-3">
              <div className="flex items-center justify-between gap-3 text-xs text-primary">
                <span className="flex min-w-0 items-center gap-2">
                  <RefreshCw className="h-4 w-4 shrink-0 animate-spin" />
                  <span className="truncate">{p.trainStatus?.message || "准备中…"}</span>
                </span>
                <span className="shrink-0 font-mono">{Math.round(p.trainStatus?.percent ?? 0)}%</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-primary/20">
                <div
                  className="h-full rounded-full bg-primary transition-[width] duration-700"
                  style={{ width: `${Math.max(2, p.trainStatus?.percent ?? 0)}%` }}
                />
              </div>
              <ol className="grid grid-cols-5 gap-1.5">
                {["预处理", "提音高", "提特征", "模型训练", "建索引"].map((name, i) => {
                  const cur = p.trainStatus?.stage_index ?? -1
                  return (
                    <li key={name} className={`rounded-md border px-1 py-1.5 text-center text-[10px] leading-tight ${i < cur ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600" : i === cur ? "border-primary bg-primary/10 text-primary font-medium" : "border-border text-muted-foreground"}`}>
                      {i < cur ? "✓ " : ""}{name}
                    </li>
                  )
                })}
              </ol>
              {p.trainStatus?.log_tail?.length ? (
                <pre className="max-h-32 overflow-y-auto whitespace-pre-wrap break-all rounded-md border border-border bg-background/70 p-2 font-mono text-[10px] leading-4 text-muted-foreground">
                  {p.trainStatus.log_tail.join("\n")}
                </pre>
              ) : null}
              <p className="text-[11px] text-muted-foreground">训练全程后台进行，可关闭页面；完成或失败后这里会自动更新。</p>
            </div>
          ) : p.trainStatus?.error ? (
            <div className="mt-5 space-y-3">
              <p className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive">
                <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />上次训练失败：{p.trainStatus.error}
              </p>
              {p.trainStatus.log_tail?.length ? (
                <pre className="max-h-28 overflow-y-auto whitespace-pre-wrap break-all rounded-md border border-border bg-background/70 p-2 font-mono text-[10px] leading-4 text-muted-foreground">
                  {p.trainStatus.log_tail.slice(-8).join("\n")}
                </pre>
              ) : null}
              <TrainButton p={p} label="重新训练" />
            </div>
          ) : p.modelOk && !p.trainStatus?.running ? (
            <div className="mt-5 space-y-3">
              {p.trainStatus?.done && (
                <p className="flex items-center gap-2 text-xs text-emerald-600">
                  <CheckCircle2 className="h-3.5 w-3.5" />最近一次训练已完成，可直接开始实时变声。
                </p>
              )}
              <TrainButton p={p} label="模型已就绪 · 重新训练" />
            </div>
          ) : (
            <div className="mt-5 space-y-3">
              <TrainButton p={p} label="训练模型" />
              {!p.modelOk && !p.trainingRunning && !p.trainStatus?.error && (
                <p className="flex items-center gap-2 text-xs text-muted-foreground">
                  <CircleAlert className="h-3.5 w-3.5" />还没有可用模型，先点击上方按钮训练（约需 30–60 分钟）。
                </p>
              )}
            </div>
          )}
        </section>
      </main>

      {p.message && (
        <div className="mx-auto max-w-7xl px-5 pb-10 sm:px-8 lg:px-12">
          <p className="rounded-md border border-border bg-card/70 px-3 py-2.5 text-xs leading-5 text-card-foreground">{p.message}</p>
        </div>
      )}
    </div>
  )
}
