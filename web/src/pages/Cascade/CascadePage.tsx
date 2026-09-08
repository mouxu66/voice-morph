import {
  AudioWaveform,
  CheckCircle2,
  CircleAlert,
  Cpu,
  Gauge,
  Languages,
  Loader2,
  Mic,
  Play,
  Sparkles,
  Square,
  Volume2,
  Waves,
} from "lucide-react"
import type { ReactNode } from "react"
import type { useCascade } from "@/pages/Cascade/useCascade"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"
import { VoiceSourceBadge } from "@/components/voice-studio/VoiceSourceBadge"

const STAGE_LABEL: Record<string, string> = {
  idle: "未启动",
  init: "初始化",
  warming: "模型预热",
  capturing: "聆听中",
  asr: "识别中",
  tts: "合成中",
  playing: "播放中",
  error: "出错",
}

function StatusBadge({ ok, tone = "auto", children }: { ok: boolean; tone?: "auto" | "warn" | "busy"; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
        ok
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600"
          : tone === "warn"
            ? "border-amber-500/40 bg-amber-500/10 text-amber-500"
            : tone === "busy"
              ? "border-primary/40 bg-primary/10 text-primary"
              : "border-muted bg-muted/40 text-muted-foreground",
      )}
    >
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : tone === "busy" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CircleAlert className="h-3.5 w-3.5" />}
      {children}
    </span>
  )
}

/** 链路图的一跳：麦克风 → 识别 → 文字 → 合成 → CABLE */
function ChainNode({
  icon: Icon,
  label,
  state,
}: {
  icon: typeof Mic
  label: string
  state: "done" | "active" | "idle"
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-col items-center gap-1.5">
      <span
        className={cn(
          "flex h-10 w-10 items-center justify-center rounded-xl border transition",
          state === "active" && "border-primary bg-primary text-primary-foreground shadow-md",
          state === "done" && "border-emerald-500/40 bg-emerald-500/10 text-emerald-600",
          state === "idle" && "border-border bg-muted/40 text-muted-foreground",
        )}
      >
        <Icon className={cn("h-4 w-4", state === "active" && "animate-pulse")} />
      </span>
      <span className={cn("text-[10px] font-medium leading-tight", state === "idle" ? "text-muted-foreground" : "text-card-foreground")}>
        {label}
      </span>
    </div>
  )
}

function ChainArrow({ on }: { on: boolean }) {
  return <span className={cn("mt-4 h-px w-full min-w-3 flex-1 border-t border-dashed", on ? "border-primary/70" : "border-border")} />
}

/** 状态/指标小卡 */
function Stat({ label, value, tone = "default" }: { label: string; value: string; tone?: "default" | "good" | "warn" | "bad" }) {
  return (
    <div className="rounded-xl border border-border bg-background/60 px-3 py-2.5">
      <p className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">{label}</p>
      <p
        className={cn(
          "mt-1 font-mono text-sm font-semibold",
          tone === "good" && "text-emerald-600",
          tone === "warn" && "text-amber-500",
          tone === "bad" && "text-destructive",
          tone === "default" && "text-card-foreground",
        )}
      >
        {value}
      </p>
    </div>
  )
}

function latencyTone(s: number | undefined): "good" | "warn" | "bad" | "default" {
  if (!s) return "default"
  if (s <= 2.5) return "good"
  if (s <= 4) return "warn"
  return "bad"
}

export function CascadePage(p: ReturnType<typeof useCascade>) {
  const s = p.status
  const running = p.running
  const stage = running ? (s?.stage ?? "init") : "idle"
  const stageLabel = STAGE_LABEL[stage] ?? stage
  const workerReady = Boolean(s?.worker_ready)
  const warming = Boolean(s?.warming) || stage === "warming"
  const errorText = s?.child_error || s?.last_error || (stage === "error" ? "级联子进程异常退出" : "")
  const avgLat = s?.avg_latency_s ?? 0
  const latencyHint = !running || !avgLat ? "" : avgLat > 3.5 ? "滞后偏高：检查是否有游戏/Epic 等程序占用 GPU" : avgLat <= 2.5 ? "达到验收标准（≤2.5s）" : "轻微积压，属正常波动"

  // 链路高亮：正在哪个环节，之前的一律视为已完成
  const order = ["capturing", "asr", "tts", "playing"]
  const cur = order.indexOf(stage)
  // 本次页面会话还没跑过（status 可能是上次会话的残留），指标先不显示
  const hasSession = running || p.transcripts.length > 0

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">CASCADE / TEXT-RELAY VOICE</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">级联变声</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            说话 → 识别成文字 → 用目标音色重新合成 → 虚拟声卡。走文字中转，你的口音与发音习惯完全不进入输出，
            这是 RVC 直接转换做不到的。说完一句约 1.5~2s 后播出，适合通话；微信里把录音设备指向 CABLE Output 即可。
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <StatusBadge ok={workerReady} tone={warming ? "busy" : "warn"}>
              TTS 引擎{workerReady ? "就绪" : warming ? "加载中" : "未启动"}
            </StatusBadge>
            <StatusBadge ok={running}>{running ? `运行中 · ${stageLabel}` : "未启动"}</StatusBadge>
            <StatusBadge ok={hasSession && Boolean(avgLat && avgLat <= 2.5)} tone={hasSession && avgLat > 2.5 ? "warn" : "auto"}>
              {hasSession && avgLat ? `平均滞后 ${avgLat}s` : "平均滞后 —"}
            </StatusBadge>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-8 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {errorText && (
          <ErrorPanel title="级联变声异常" detail={errorText} hint="可重试启动；仍失败请查看 outputs/cascade_run.log 的完整日志" />
        )}

        {/* 01 选音色 */}
        <section>
          <div className="mb-3 flex items-end justify-between gap-3">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">01 / 选音色</p>
              <h3 className="mt-1 text-lg font-semibold text-card-foreground">用哪个音色说话</h3>
            </div>
            <p className="hidden text-xs text-muted-foreground sm:block">
              用音色库的参考音频（TTS 克隆），无需训练 RVC 模型
            </p>
          </div>
          {p.voices.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-border bg-card/60 p-6 text-center">
              <Sparkles className="mx-auto h-6 w-6 text-muted-foreground" />
              <p className="mt-3 text-sm font-medium text-card-foreground">音色库还是空的</p>
              <p className="mx-auto mt-1.5 max-w-md text-xs leading-5 text-muted-foreground">
                先到「音色库」或「音色微调」准备一个音色档案。不选也可以直接启动——会用内置的默认音色（美团鼠鼠）。
              </p>
            </div>
          ) : (
            <div className="-mx-1 flex snap-x gap-3 overflow-x-auto px-1 pb-2">
              {p.voices.map((v) => {
                const active = v.id === p.selectedVoice
                return (
                  <button
                    key={v.id}
                    type="button"
                    onClick={() => p.selectVoice(v.id)}
                    disabled={running}
                    aria-pressed={active}
                    className={cn(
                      "flex w-52 shrink-0 snap-start flex-col items-start gap-2 rounded-xl border p-3.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-60",
                      active
                        ? "border-primary bg-primary/10 shadow-[0_0_0_1px_rgb(var(--c-accent)/0.35)]"
                        : "border-border bg-card hover:border-primary/50",
                    )}
                  >
                    <div className="flex w-full items-center justify-between gap-2">
                      <span
                        className={cn(
                          "flex h-9 w-9 items-center justify-center rounded-lg",
                          active ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground",
                        )}
                      >
                        <AudioWaveform className="h-4 w-4" />
                      </span>
                      {active && <CheckCircle2 className="h-4 w-4 text-primary" />}
                    </div>
                    <div className="flex w-full items-center gap-1.5">
                      <p className="min-w-0 flex-1 truncate text-sm font-semibold text-card-foreground" title={v.display_name ?? v.id}>
                        {v.display_name ?? v.id}
                      </p>
                      <VoiceSourceBadge voice={v} />
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                        {v.kind === "finetuned" ? "微调音色" : "克隆音色"}
                      </span>
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
                        参考 {v.duration_s.toFixed(1)}s
                      </span>
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </section>

        <div className="grid gap-8 lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
          {/* 02 控制台 */}
          <section className="flex flex-col gap-5 rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">02 / 变声</p>
              <h3 className="mt-1 flex items-center gap-2 text-lg font-semibold text-card-foreground">
                <Waves className={cn("h-4 w-4 text-primary", running && "animate-pulse")} />
                级联变声控制台
              </h3>
              <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                {running
                  ? `正在聆听（${s?.input_device || "麦克风"}）。对着麦克风正常说话，说完一句稍候即播出目标音色。全局热键 Ctrl+Alt+V 可一键停止。`
                  : "启动后系统录音设备自动切到 CABLE Output；点「停止」自动还原。全局热键 Ctrl+Alt+V 一键启停（复用上次音色与参数），与「实时变声」互斥。"}
              </p>
            </div>

            {running ? (
              <button
                type="button"
                onClick={p.stop}
                disabled={p.stopping}
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-destructive px-4 py-3.5 text-sm font-medium text-destructive-foreground shadow-md transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-60"
              >
                {p.stopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                {p.stopping ? "正在停止并还原声卡…" : "停止并还原声卡"}
              </button>
            ) : (
              <button
                type="button"
                onClick={p.start}
                disabled={p.starting}
                className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3.5 text-sm font-medium text-primary-foreground shadow-md transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
              >
                {p.starting || warming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                {p.starting ? "正在启动…" : warming ? "模型加载中，就绪后自动开始…" : "开始级联变声"}
              </button>
            )}

            {/* 链路图 */}
            <div className="rounded-xl border border-border bg-background/50 px-4 py-4">
              <div className="flex items-center gap-1.5">
                <ChainNode icon={Mic} label="麦克风采集" state={cur === 0 ? "active" : cur > 0 ? "done" : "idle"} />
                <ChainArrow on={cur >= 0 && running} />
                <ChainNode icon={Languages} label="语音识别" state={cur === 1 ? "active" : cur > 1 ? "done" : "idle"} />
                <ChainArrow on={cur >= 1} />
                <ChainNode icon={Gauge} label={`音色合成${s?.last_fast ? "·加速" : ""}`} state={cur === 2 ? "active" : cur > 2 ? "done" : "idle"} />
                <ChainArrow on={cur >= 2} />
                <ChainNode icon={Volume2} label="CABLE 虚拟声卡" state={cur === 3 ? "active" : "idle"} />
              </div>
              <p className="mt-3 text-center font-mono text-[10px] text-muted-foreground">
                {running ? `当前环节：${stageLabel}` : "等待启动"}
              </p>
            </div>

            {/* 参数 */}
            <div className="space-y-4">
              <div>
                <p className="text-xs font-medium text-card-foreground">出声方式</p>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {([
                    { key: "stream", label: "按句流式", hint: "说完一句就合成一句，滞后约 1.5~2s，适合通话" },
                    { key: "whole", label: "整段说完", hint: "等你说完整段再合成，更连贯但滞后更高，适合发语音" },
                  ] as const).map((m) => (
                    <button
                      key={m.key}
                      type="button"
                      onClick={() => p.setMode(m.key)}
                      disabled={running}
                      aria-pressed={p.mode === m.key}
                      className={cn(
                        "rounded-xl border px-3 py-2.5 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-60",
                        p.mode === m.key
                          ? "border-primary bg-primary/10"
                          : "border-border bg-card hover:border-primary/50",
                      )}
                    >
                      <span className={cn("text-xs font-semibold", p.mode === m.key ? "text-primary" : "text-card-foreground")}>
                        {m.label}
                      </span>
                      <span className="mt-1 block text-[10px] leading-4 text-muted-foreground">{m.hint}</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className={cn(p.mode !== "stream" && "opacity-50")}>
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs font-medium text-card-foreground">单句上限（流式模式）</p>
                  <span className="font-mono text-xs text-primary">{p.chunkMaxS.toFixed(1)}s</span>
                </div>
                <input
                  type="range"
                  min={2}
                  max={12}
                  step={0.5}
                  value={p.chunkMaxS}
                  disabled={running || p.mode !== "stream"}
                  onChange={(e) => p.setChunkMaxS(Number(e.target.value))}
                  className="mt-2 w-full accent-[hsl(var(--primary))]"
                />
                <p className="mt-1 text-[10px] leading-4 text-muted-foreground">
                  超过该时长的句子会被强制切开。切得越碎延迟越低但韵律越顿，默认 6s 是折中值。
                </p>
              </div>
            </div>

            {running && (
              <p className="flex items-start gap-2 text-xs leading-5 text-primary">
                <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                变声中：点「停止」或异常退出都会自动还原设备；若忘了停，随时到左侧栏点「一键恢复音频」。
              </p>
            )}
          </section>

          {/* 03 实时状态 */}
          <section className="flex flex-col gap-4 rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div>
              <p className="font-mono text-xs uppercase tracking-widest text-primary">03 / 实时状态</p>
              <h3 className="mt-1 text-lg font-semibold text-card-foreground">端到端滞后与耗时</h3>
              <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                滞后口径：说完一句话 → 开始听到目标音色。连续说话不积压就不会增长。
              </p>
            </div>

            <div className="grid grid-cols-2 gap-2.5">
              <Stat label="最近滞后" value={hasSession && s?.last_latency_s ? `${s.last_latency_s}s` : "—"} tone={latencyTone(hasSession ? s?.last_latency_s : undefined)} />
              <Stat label="平均滞后" value={hasSession && avgLat ? `${avgLat}s` : "—"} tone={latencyTone(hasSession ? avgLat || undefined : undefined)} />
              <Stat
                label="识别耗时 avg/p95"
                value={hasSession && s?.avg_asr_s ? `${s.avg_asr_s}s / ${s.p95_asr_s ?? "—"}s` : "—"}
                tone={s && s.p95_asr_s > 3 ? "warn" : "default"}
              />
              <Stat
                label="合成耗时 avg/p95"
                value={hasSession && s?.avg_tts_s ? `${s.avg_tts_s}s / ${s.p95_tts_s ?? "—"}s` : "—"}
                tone={s && s.p95_tts_s > 4 ? "warn" : "default"}
              />
              <Stat label="待播积压" value={hasSession && s?.queued_s ? `${s.queued_s}s` : "—"} tone={s && s.queued_s > 6 ? "warn" : "default"} />
              <Stat label="已合成句子" value={hasSession ? String(s?.chunks ?? 0) : "—"} />
            </div>

            {latencyHint && (
              <p
                className={cn(
                  "flex items-start gap-2 rounded-md px-3 py-2 text-[11px] leading-4",
                  avgLat > 3.5 ? "bg-amber-500/10 text-amber-500" : "bg-emerald-500/10 text-emerald-600",
                )}
              >
                <Cpu className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {latencyHint}
              </p>
            )}

            {/* 识别文字流 */}
            <div className="min-w-0">
              <p className="text-xs font-medium text-card-foreground">识别文字流</p>
              <div className="mt-2 max-h-56 overflow-y-auto rounded-xl border border-border bg-background/60 p-3">
                {p.transcripts.length === 0 ? (
                  <p className="py-4 text-center text-[11px] text-muted-foreground">
                    {running ? "说话后这里显示识别出的文字，便于发现识别错误" : "启动后开始显示"}
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {p.transcripts.map((t) => (
                      <li key={t.id} className="flex items-start gap-2 text-[11px] leading-5">
                        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">#{t.id}</span>
                        <span className="min-w-0 text-card-foreground">{t.text}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            {running && (
              <dl className="space-y-1 border-t border-border pt-3 font-mono text-[10px] text-muted-foreground">
                <div className="flex justify-between gap-2">
                  <dt>输入</dt>
                  <dd className="truncate text-right" title={s?.input_device}>{s?.input_device || "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>输出</dt>
                  <dd className="truncate text-right" title={s?.output_device}>{s?.output_device || "—"}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>合成引擎</dt>
                  <dd>{s?.last_fast === null || s?.last_fast === undefined ? "—" : s.last_fast ? "CUDA Graph 加速" : "原版（性能不足请检查）"}</dd>
                </div>
              </dl>
            )}
          </section>
        </div>

        {p.feedback && (
          p.feedback.tone === "error"
            ? <ErrorPanel title="级联操作失败" detail={p.feedback.text} />
            : <p className={cn("rounded-xl border px-4 py-3 text-xs leading-5",
                p.feedback.tone === "ok" && "border-emerald-500/30 bg-emerald-500/10 text-emerald-600",
                p.feedback.tone === "info" && "border-border bg-card/70 text-card-foreground")}>{p.feedback.text}</p>
        )}
      </main>
    </div>
  )
}
