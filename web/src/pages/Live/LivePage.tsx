import {
  ArrowDown,
  CheckCircle2,
  CircleAlert,
  Database,
  Download,
  Loader2,
  Mic2,
  Play,
  Radio,
  RefreshCw,
  Square,
  Upload,
  WandSparkles,
} from "lucide-react"
import type { ReactNode } from "react"
import type { useLive } from "@/pages/Live/useLive"
import { ErrorPanel } from "@/components/ErrorPanel"
import { LiveLevelMeter } from "@/components/voice-studio/LiveLevelMeter"
import { RvcChainDiagram } from "@/components/voice-studio/RvcChainDiagram"
import { RvcVoicePicker } from "@/components/voice-studio/RvcVoicePicker"
import { TrainLogConsole } from "@/components/voice-studio/TrainLogConsole"
import { cn } from "@/lib/utils"

function StatusBadge({ ok, tone = "auto", children }: { ok: boolean; tone?: "auto" | "warn"; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        ok
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600"
          : tone === "warn"
            ? "border-amber-500/40 bg-amber-500/10 text-amber-500"
            : "border-muted bg-muted/40 text-muted-foreground",
      )}
    >
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <CircleAlert className="h-3.5 w-3.5" />}
      {children}
    </span>
  )
}

/** 训练流水线的一个步骤：序号 + 名称 + 说明 + 右侧操作 */
function PipelineStep({
  index,
  label,
  hint,
  done,
  active,
  action,
}: {
  index: number
  label: string
  hint: string
  done: boolean
  active: boolean
  action: ReactNode
}) {
  return (
    <li className="flex items-start gap-3">
      <div className="flex flex-col items-center self-stretch">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-semibold transition",
            done
              ? "bg-emerald-500 text-white"
              : active
                ? "bg-primary text-primary-foreground"
                : "bg-muted text-muted-foreground",
          )}
        >
          {done ? <CheckCircle2 className="h-4 w-4" /> : index}
        </span>
        <span className={cn("mt-1 w-px flex-1", done ? "bg-emerald-500/40" : "bg-border")} />
      </div>
      <div className="min-w-0 flex-1 pb-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className={cn("text-sm font-medium", done ? "text-card-foreground" : active ? "text-card-foreground" : "text-muted-foreground")}>
            {label}
          </p>
          {action}
        </div>
        <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{hint}</p>
      </div>
    </li>
  )
}

function ActionButton({
  onClick,
  disabled,
  busy,
  busyLabel,
  icon: Icon,
  children,
  tone = "default",
}: {
  onClick: () => void
  disabled?: boolean
  busy?: boolean
  busyLabel?: string
  icon: typeof Play
  children: ReactNode
  tone?: "default" | "primary"
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || busy}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50",
        tone === "primary"
          ? "bg-primary text-primary-foreground hover:bg-primary/90"
          : "border border-primary/40 bg-primary/10 text-primary hover:bg-primary/20",
      )}
    >
      {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className="h-3.5 w-3.5" />}
      {busy ? busyLabel : children}
    </button>
  )
}

export function LivePage(p: ReturnType<typeof useLive>) {
  const voiceName = p.current?.display_name ?? null
  // 变声按钮：模型就绪就能开；若正在跑的是别的音色，需先停止才能切
  const liveOnOtherVoice = p.liveOn && p.liveExp && p.liveExp !== p.selectedExp
  const startDisabled = p.starting || (!p.modelOk && !p.liveOn) || Boolean(liveOnOtherVoice)
  const trainRunning = Boolean(p.trainStatus?.running)
  const genRunning = Boolean(p.genStatus?.running)
  const logLines = p.trainStatus?.log_tail ?? []

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">REALTIME / VOICE CHANGER</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">实时变声</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            选一个音色 → 给它训练 RVC 模型 → 一键开启实时变声。开启后系统录音会自动切到虚拟声卡，
            微信/游戏里说出来的就是该音色的声音；停止后自动还原设备。
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <StatusBadge ok={Boolean(p.voicesInfo?.rvc_ready)} tone="warn">
              RVC 环境{p.voicesInfo?.rvc_ready ? "已就绪" : "未就绪"}
            </StatusBadge>
            <StatusBadge ok={Boolean(voiceName)}>{voiceName ? `当前音色：${voiceName}` : "未选择音色"}</StatusBadge>
            <StatusBadge ok={p.modelOk}>RVC 模型{p.modelOk ? "已就绪" : "未训练"}</StatusBadge>
            <StatusBadge ok={p.liveOn}>实时{p.liveOn ? "运行中" : "未启动"}</StatusBadge>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-8 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {p.voicesInfo && !p.voicesInfo.rvc_ready && (
          <p className="flex items-start gap-2 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-xs leading-5 text-amber-500">
            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            未检测到 RVC 整合包（{p.voicesInfo.rvc_root}）。实时变声依赖它，请先用环境变量 VM_RVC_ROOT 指向你的 RVC 目录，或把整合包放到该路径。
          </p>
        )}

        {/* 01 选音色 —— 实时页以前没有这一步，导致"选了却不像" */}
        <section>
          <div className="mb-3 flex items-end justify-between gap-3">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">01 / 选音色</p>
              <h3 className="mt-1 text-lg font-semibold text-card-foreground">用哪个音色变声</h3>
            </div>
            <p className="hidden text-xs text-muted-foreground sm:block">
              实时变声用的是这里选中的音色模型，与音色库的「当前使用」互相独立
            </p>
          </div>
          <RvcVoicePicker
            voices={p.voices}
            selectedId={p.selectedExp}
            onSelect={p.selectExp}
            liveExp={p.liveExp}
          />
        </section>

        <div className="grid gap-8 lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
          {/* 02 变声控制台 */}
          <section className="flex flex-col gap-4 rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">02 / 变声</p>
              <h3 className="mt-1 flex items-center gap-2 text-lg font-semibold text-card-foreground">
                <Radio className={cn("h-4 w-4", p.liveOn ? "animate-pulse text-primary" : "text-primary")} />
                实时变声控制台
              </h3>
              <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                {voiceName
                  ? p.liveOn
                    ? `正在用「${voiceName}」变声。对着麦克风说话，微信/游戏里就是它的声音。`
                    : p.modelOk
                      ? `「${voiceName}」模型已就绪，点下面按钮开始。`
                      : `「${voiceName}」还没训练模型，请先在右侧训练。`
                  : "先在上方选一个音色。"}
              </p>
            </div>

            {p.liveOn ? (
              <button
                type="button"
                onClick={p.stop}
                disabled={p.stopping}
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-destructive px-4 py-3.5 text-sm font-medium text-destructive-foreground shadow-md transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-60"
              >
                {p.stopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                {p.stopping ? "正在停止并还原声卡…" : "停止实时变声"}
              </button>
            ) : (
              <button
                type="button"
                onClick={p.start}
                disabled={startDisabled}
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3.5 text-sm font-medium text-primary-foreground shadow-md transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
              >
                {p.starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                {p.starting ? "正在启动…" : "开始实时变声"}
              </button>
            )}

            {liveOnOtherVoice && (
              <p className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs leading-5 text-amber-500">
                <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                当前正在用「{p.liveExp}」变声。要换成「{voiceName}」，请先点「停止实时变声」再启动。
              </p>
            )}

            <LiveLevelMeter active={p.liveOn} />

            <RvcChainDiagram
              inputDevice={p.liveStatus?.input_device ?? "麦克风阵列"}
              outputDevice={p.liveStatus?.output_device ?? "CABLE Input"}
              liveRunning={p.liveOn}
              modelReady={p.modelOk}
              audioSwitched={Boolean(p.liveStatus?.audio_switched)}
              exp={p.selectedExp ?? ""}
            />

            {p.liveOn && (
              <p className="flex items-start gap-2 text-xs leading-5 text-primary">
                <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                变声中：关闭 RVC 窗口或点「停止」会自动还原设备；若忘了关，随时到左侧栏点「一键恢复音频」。
              </p>
            )}
            {!p.liveOn && p.liveStatus?.last_error && (
              <ErrorPanel title="上次还原声卡失败" detail={p.liveStatus.last_error} hint="请点左侧栏「一键恢复音频」重试" />
            )}
          </section>

          {/* 03 训练流水线 */}
          <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">03 / 训练</p>
            <h3 className="mt-1 flex items-center gap-2 text-lg font-semibold text-card-foreground">
              <Mic2 className="h-4 w-4 text-primary" />
              {voiceName ? `为「${voiceName}」准备模型` : "准备模型"}
            </h3>
            <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
              按顺序走完三步，模型就绪后就能一键变声。全程后台进行，可以离开这个页面。
            </p>

            <ol className="mt-5">
              <PipelineStep
                index={1}
                label="生成语料"
                hint={
                  genRunning
                    ? p.genStatus?.current || "正在合成…"
                    : p.generatedCount > 0
                      ? `已生成 ${p.generatedCount} 句训练语料`
                      : "用该音色的参考音频合成一批训练句"
                }
                done={p.generatedCount > 0}
                active={p.activeStep === 0}
                action={
                  <ActionButton
                    onClick={p.generateCorpus}
                    disabled={!p.selectedExp || !p.current?.has_reference || genRunning || trainRunning}
                    busy={p.generating || genRunning}
                    busyLabel="生成中…"
                    icon={WandSparkles}
                  >
                    {p.generatedCount > 0 ? "重新生成" : "生成语料"}
                  </ActionButton>
                }
              />
              {genRunning && (
                <div className="ml-10 -mt-3 mb-4">
                  <div className="h-1.5 overflow-hidden rounded-full bg-primary/20">
                    <div
                      className="h-full rounded-full bg-primary transition-[width] duration-500"
                      style={{
                        width: `${Math.max(3, ((p.genStatus?.done ?? 0) / Math.max(1, p.genStatus?.total ?? 1)) * 100)}%`,
                      }}
                    />
                  </div>
                  <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                    {p.genStatus?.done ?? 0} / {p.genStatus?.total ?? 0}
                  </p>
                </div>
              )}

              <PipelineStep
                index={2}
                label="导入 RVC"
                hint={
                  p.datasetCount > 0
                    ? `训练集目录已有 ${p.datasetCount} 条语料`
                    : "把语料同步到 RVC 的训练集目录"
                }
                done={p.datasetCount > 0}
                active={p.activeStep === 1}
                action={
                  <ActionButton
                    onClick={p.importCorpus}
                    disabled={!p.selectedExp || p.generatedCount === 0 || genRunning || trainRunning}
                    busy={p.importing}
                    busyLabel="导入中…"
                    icon={Upload}
                  >
                    {p.datasetCount > 0 ? "重新导入" : "导入 RVC"}
                  </ActionButton>
                }
              />

              <PipelineStep
                index={3}
                label="训练模型"
                hint={
                  trainRunning
                    ? p.trainStatus?.message || "准备中…"
                    : p.modelOk
                      ? p.current?.trained_at
                        ? `模型已就绪（训练于 ${p.current.trained_at}）`
                        : "模型已就绪"
                      : p.trainStatus?.error
                        ? "上次训练失败，可重试"
                        : "预处理 → 提音高 → 提特征 → 训练 → 建索引"
                }
                done={p.modelOk}
                active={p.activeStep === 2}
                action={
                  <ActionButton
                    onClick={p.train}
                    disabled={!p.selectedExp || p.datasetCount === 0 || trainRunning || p.liveOn}
                    busy={p.training || trainRunning}
                    busyLabel="训练中…"
                    icon={RefreshCw}
                    tone={p.modelOk ? "default" : "primary"}
                  >
                    {trainRunning ? "训练中" : p.modelOk ? "重新训练" : "开始训练"}
                  </ActionButton>
                }
              />
              {trainRunning && (
                <div className="ml-10 -mt-3 mb-4 space-y-2">
                  <div className="flex items-center justify-between gap-2 text-[11px] text-primary">
                    <span className="truncate">{p.trainStatus?.message || "准备中…"}</span>
                    <span className="shrink-0 font-mono">{Math.round(p.trainStatus?.percent ?? 0)}%</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-primary/20">
                    <div
                      className="h-full rounded-full bg-primary transition-[width] duration-700"
                      style={{ width: `${Math.max(3, p.trainStatus?.percent ?? 0)}%` }}
                    />
                  </div>
                  <ol className="grid grid-cols-5 gap-1">
                    {["预处理", "提音高", "提特征", "模型训练", "建索引"].map((name, i) => {
                      const cur = p.trainStatus?.stage_index ?? -1
                      return (
                        <li
                          key={name}
                          className={cn(
                            "rounded-md border px-1 py-1 text-center text-[9px] leading-tight",
                            i < cur
                              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600"
                              : i === cur
                                ? "border-primary bg-primary/10 font-medium text-primary"
                                : "border-border text-muted-foreground",
                          )}
                        >
                          {i < cur ? "✓ " : ""}
                          {name}
                        </li>
                      )
                    })}
                  </ol>
                </div>
              )}
              {!trainRunning && p.trainStatus?.error && (
                <ErrorPanel title="上次 RVC 训练失败" detail={p.trainStatus.error} />
              )}

              <PipelineStep
                index={4}
                label="实时变声"
                hint={p.liveOn ? "正在变声中" : "模型就绪后回到左侧开启"}
                done={p.liveOn}
                active={p.activeStep === 3}
                action={
                  <span className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground">
                    <ArrowDown className="h-3.5 w-3.5" />
                    见左侧控制台
                  </span>
                }
              />
            </ol>

            {!p.current?.has_reference && p.selectedExp && (
              <p className="flex items-start gap-2 rounded-md border border-border bg-background/60 px-3 py-2.5 text-[11px] leading-4 text-muted-foreground">
                <Database className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                这个音色只有训练产物、没有参考音频，因此无法新成语料；可直接用已有语料训练或重新训练。
              </p>
            )}
            {p.datasetCount === 0 && p.generatedCount === 0 && !trainRunning && (
              <p className="flex items-start gap-2 rounded-md border border-border bg-background/60 px-3 py-2.5 text-[11px] leading-4 text-muted-foreground">
                <Download className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                训练大约需要 30–60 分钟，期间可以关掉页面去干别的，回来进度还在。
              </p>
            )}
          </section>
        </div>

        {/* 04 训练日志 */}
        {(logLines.length > 0 || trainRunning) && (
          <section>
            <div className="mb-3">
              <p className="font-mono text-xs uppercase tracking-widest text-primary">04 / 日志</p>
              <h3 className="mt-1 text-lg font-semibold text-card-foreground">训练输出</h3>
            </div>
            <TrainLogConsole lines={logLines} running={trainRunning} />
          </section>
        )}

        {p.genStatus?.error && (
          <ErrorPanel title="语料生成失败" detail={p.genStatus.error} />
        )}

        {p.feedback && (
          p.feedback.tone === "error"
            ? <ErrorPanel title="实时变声操作失败" detail={p.feedback.text} />
            : <p className={cn("rounded-xl border px-4 py-3 text-xs leading-5",
                p.feedback.tone === "ok" && "border-emerald-500/30 bg-emerald-500/10 text-emerald-600",
                p.feedback.tone === "info" && "border-border bg-card/70 text-card-foreground")}>{p.feedback.text}</p>
        )}
      </main>
    </div>
  )
}
