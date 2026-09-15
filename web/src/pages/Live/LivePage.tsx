import {
  ArrowDown,
  AudioLines,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Database,
  Download,
  Gauge,
  Loader2,
  Mic,
  Mic2,
  Play,
  Radio,
  RefreshCw,
  Sparkles,
  Square,
  Terminal,
  Upload,
  WandSparkles,
  Wrench,
} from "lucide-react"
import { useEffect, useRef, useState, type ReactNode } from "react"
import { useLive } from "@/pages/Live/useLive"
import { ErrorPanel } from "@/components/ErrorPanel"
import { EffectLadderCard } from "@/components/EffectLadderCard"
import type { SendChainInfo } from "@/types"
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

/** 发送链路自检结果：全绿一行收拢；有红/黄问题逐条列出并给修复入口 */
function ChainResultList(p: {
  chain: SendChainInfo
  micMissing: boolean
  fixingKey: string | null
  onFix: (key: string) => void
}) {
  const issues = p.chain.items.filter((it) => !it.ok)
  const allOk = issues.length === 0 && !p.micMissing
  if (allOk) {
    return (
      <p className="mt-4 flex items-center gap-2 text-xs text-emerald-600">
        <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
        链路已就绪：虚拟声卡、默认录音、播放设备、麦克风都正常，可以放心开麦。
      </p>
    )
  }
  return (
    <ul className="mt-4 space-y-2">
      {issues.map((it) => (
        <li key={it.key}
          className={cn("flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2.5 text-xs",
            it.warn ? "border-amber-500/40 bg-amber-500/10" : "border-destructive/40 bg-destructive/10")}>
          <CircleAlert className={cn("h-3.5 w-3.5 shrink-0", it.warn ? "text-amber-500" : "text-destructive")} />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-card-foreground">{it.label}：</span>
            {it.detail}
            {it.hint && <span className="mt-0.5 block leading-4 text-muted-foreground">{it.hint}</span>}
          </span>
          {(it.key === "default_capture" || it.key === "stale_backup") && (
            <button type="button" onClick={() => p.onFix(it.key)} disabled={p.fixingKey !== null}
              className="inline-flex shrink-0 items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1.5 font-medium text-primary transition hover:bg-primary/20 disabled:pointer-events-none disabled:opacity-50">
              {p.fixingKey === it.key ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wrench className="h-3 w-3" />}
              {p.fixingKey === it.key ? "修复中…" : it.key === "default_capture" ? "一键最优" : "恢复默认设备"}
            </button>
          )}
        </li>
      ))}
      {p.micMissing && (
        <li className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs">
          <CircleAlert className="h-3.5 w-3.5 shrink-0 text-amber-500" />
          <span className="min-w-0 flex-1">
            <span className="font-medium text-card-foreground">麦克风：</span>没检测到录音设备
            <span className="mt-0.5 block leading-4 text-muted-foreground">插上麦克风 / 手机后，点下方「刷新设备列表」，再等自检复检。</span>
          </span>
        </li>
      )}
    </ul>
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
  // GPU 显存占用百分比（供性能卡片显存条使用）
  const gpuPct =
    p.gpuTotalMb && p.gpuUsedMb
      ? Math.min(100, Math.round((p.gpuUsedMb / p.gpuTotalMb) * 100))
      : 0

  // 训练日志默认收起，不占页面空间；出现新错误时自动弹出
  const [logOpen, setLogOpen] = useState(false)
  const logErrRef = useRef<string | null>(null)
  const logError = p.trainStatus?.error ?? ""
  useEffect(() => {
    if (logError && logError !== logErrRef.current) {
      logErrRef.current = logError
      setLogOpen(true)
    }
    if (!logError) logErrRef.current = null
  }, [logError])
  const logSummary = logError
    ? "有错误，点开查看"
    : trainRunning
      ? `${p.trainStatus?.stage || "进行中"} · ${Math.round(p.trainStatus?.percent ?? 0)}%`
      : `已停止 · ${logLines.length} 行`

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

        {/* 首页「用它开麦说话」落地引导：第一次来不知道点什么，三步讲完 */}
        {p.deepLinkGuide && !p.liveOn && (
          <section className="rounded-2xl border border-primary/40 bg-primary/10 p-5 shadow-lg backdrop-blur-xl">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="flex items-center gap-2 text-sm font-semibold text-card-foreground">
                  <Sparkles className="h-4 w-4 shrink-0 text-primary" />
                  已为你选好「{voiceName ?? p.selectedExp ?? "这个音色"}」，照下面三步开麦
                </p>
                <ol className="mt-2.5 space-y-1.5 text-xs leading-5 text-muted-foreground">
                  <li className="flex gap-2"><span className="shrink-0 font-mono text-primary">1.</span>确认麦克风就是「输入麦克风」里那一个（下方可以换）</li>
                  <li className="flex gap-2"><span className="shrink-0 font-mono text-primary">2.</span>点「开始实时变声」——它会自动把微信/游戏的录音切到虚拟声卡</li>
                  <li className="flex gap-2"><span className="shrink-0 font-mono text-primary">3.</span>去微信/游戏里开麦说话；想先自己听效果，开「自我监听」</li>
                </ol>
                {/* 设备自检状态条：接自下方 SEND CHAIN 自检，有问题先修再开麦 */}
                {(p.chain || p.chainLoading) && (
                  <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-border bg-background/50 px-3 py-2.5 text-xs leading-5">
                    {p.chainLoading && !p.chain ? (
                      <span className="flex items-center gap-1.5 text-muted-foreground">
                        <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />设备自检中…
                      </span>
                    ) : (() => {
                      const issues = p.chain?.items.filter((it) => !it.ok) ?? []
                      const micMissing = p.audioDevices !== null && p.audioDevices.items.length === 0
                      const pending = issues.length + (micMissing ? 1 : 0)
                      if (pending === 0) {
                        return (
                          <span className="flex items-center gap-1.5 text-emerald-600">
                            <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />设备链路已就绪，可以直接开麦。
                          </span>
                        )
                      }
                      const shown = [...new Set([...issues.slice(0, 2).map((it) => it.label), ...(micMissing ? ["麦克风"] : [])])].join("、")
                      return (
                        <>
                          <span className="flex items-center gap-1.5 text-amber-500">
                            <CircleAlert className="h-3.5 w-3.5 shrink-0" />设备链路还有 {pending} 项待修：{shown}
                          </span>
                          <button
                            type="button"
                            onClick={() => document.getElementById("live-sendchain")?.scrollIntoView({ behavior: "smooth", block: "start" })}
                            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-primary/40 bg-primary/10 px-2.5 py-1 font-medium text-primary transition hover:bg-primary/20"
                          >
                            <Wrench className="h-3 w-3" />去修
                          </button>
                        </>
                      )
                    })()}
                  </div>
                )}
                <p className="mt-2 text-[11px] leading-4 text-muted-foreground">
                  如果变声后声音不对，随时可在左侧栏点「一键恢复音频」还原声卡设置。
                </p>
              </div>
              <button
                type="button"
                onClick={p.dismissDeepLinkGuide}
                className="shrink-0 rounded-md border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
              >
                知道了
              </button>
            </div>
          </section>
        )}

        {/* 性能模式：两档切换 + GPU 显存占用（面向边打游戏边变声的用户） */}
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="min-w-0">
              <p className="font-mono text-xs uppercase tracking-widest text-primary">PERFORMANCE</p>
              <h3 className="mt-1 flex items-center gap-2 text-lg font-semibold text-card-foreground">
                <Gauge className="h-4 w-4 text-primary" />
                性能模式
              </h3>
              <p className="mt-1.5 max-w-xl text-xs leading-5 text-muted-foreground">
                选「游戏低占用」会自动隐藏桌宠、关掉实时转写（字幕）与自我监听、卸载语音合成引擎（释放约 4.8GB 显存给游戏），并把显存探测降频到 10 秒一次，适合边打游戏边变声。
              </p>
            </div>
            <div className="flex shrink-0 rounded-lg border border-border bg-background/70 p-1">
              {([
                ["balanced", "均衡·音质优先"],
                ["game", "游戏低占用"],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  disabled={p.restarting}
                  onClick={() => void p.setProfile(value)}
                  className={cn(
                    "rounded-md px-3 py-1.5 text-xs transition disabled:pointer-events-none disabled:opacity-60",
                    p.perfProfile === value
                      ? "bg-primary text-primary-foreground shadow-md"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {p.perfProfile === "game" && p.ttsWorkerAlive === false && (
            <p className="mt-3 flex items-center gap-2 text-xs text-emerald-500">
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
              语音合成引擎已卸载，显存已让给游戏；再次使用语音合成时会按需自动加载。
            </p>
          )}

          {p.gpuTotalMb ? (
            <div className="mt-4 space-y-1.5">
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-muted-foreground">GPU 显存占用</span>
                <span className="font-mono text-card-foreground">
                  {(p.gpuUsedMb! / 1024).toFixed(1)} / {(p.gpuTotalMb / 1024).toFixed(0)} GB
                  {p.liveProcVramMb != null && (
                    <span className="ml-2 text-muted-foreground">变声进程约 {p.liveProcVramMb} MB</span>
                  )}
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-muted-foreground/30">
                <div
                  className={cn(
                    "h-full rounded-full transition-all",
                    gpuPct > 85 ? "bg-destructive" : "bg-primary",
                  )}
                  style={{ width: `${gpuPct}%` }}
                />
              </div>
            </div>
          ) : null}

          {p.restarting && (
            <p className="mt-3 flex items-center gap-2 text-xs text-primary">
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
              正按新档位重启实时变声（约 3 秒）…
            </p>
          )}
        </section>

        {/* 音频发送链路自检：进页面自动跑一遍；有问题就地给修复入口 */}
        {!p.liveOn && (p.chain || p.chainLoading) && (
          <section id="live-sendchain" className="scroll-mt-6 rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-xs uppercase tracking-widest text-primary">SEND CHAIN</p>
                <h3 className="mt-1 flex items-center gap-2 text-lg font-semibold text-card-foreground">
                  <AudioLines className="h-4 w-4 text-primary" />
                  音频发送链路自检
                </h3>
                <p className="mt-1.5 max-w-xl text-xs leading-5 text-muted-foreground">
                  进页面自动查一遍：虚拟声卡、默认录音、麦克风、播放设备有没有就位。有问题直接在这里修，修完自动复检。
                </p>
              </div>
              <button
                type="button"
                onClick={() => void p.runChainCheck()}
                disabled={p.chainLoading}
                className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
              >
                {p.chainLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                {p.chainLoading ? "检查中…" : "重新检测"}
              </button>
            </div>

            {p.chainLoading && !p.chain ? (
              <p className="mt-4 flex items-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                正在枚举音频设备（首次约 1~2 秒）…
              </p>
            ) : p.chain ? (
              <ChainResultList
                chain={p.chain}
                micMissing={p.audioDevices !== null && p.audioDevices.items.length === 0}
                fixingKey={p.fixingKey}
                onFix={(k) => void p.runChainFix(k)}
              />
            ) : null}
          </section>
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

            {p.liveOn && !p.liveReady && (
              <p className="flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2.5 text-xs leading-5 text-primary">
                <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                模型加载中（约 10~20 秒）… 就绪后对着麦克风说话即可变声。
              </p>
            )}

            {/* 自我监听：变声运行中可随时开关，让自己在耳机里听到变声效果 */}
            {p.liveOn && p.liveReady && (
              <div className="flex items-center justify-between rounded-md border border-border bg-background/60 px-3 py-2.5">
                <div className="min-w-0">
                  <p className="text-xs font-medium text-card-foreground">自我监听</p>
                  <p className="truncate text-[11px] leading-4 text-muted-foreground">
                    {p.monitorOn ? "已开：耳机里能听到变声后的自己" : "已关：变声只送给微信/游戏"}
                  </p>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={p.monitorOn}
                  onClick={() => void p.toggleMonitor(!p.monitorOn)}
                  className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${p.monitorOn ? "bg-primary" : "bg-muted-foreground/30"}`}
                >
                  <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-background shadow transition-all ${p.monitorOn ? "left-[18px]" : "left-0.5"}`}
                  />
                </button>
              </div>
            )}

            {liveOnOtherVoice && (
              <p className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2.5 text-xs leading-5 text-amber-500">
                <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                当前正在用「{p.liveExp}」变声。要换成「{voiceName}」，请先点「停止实时变声」再启动。
              </p>
            )}

            {/* A7/A8：输入麦克风选择 + 输入降噪 —— 未启动时也可设置，启动后按所选设备采音 */}
            <div className="space-y-3 rounded-md border border-border bg-background/60 px-3 py-3">
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="flex items-center gap-1.5 text-xs font-medium text-card-foreground">
                    <Mic className="h-3.5 w-3.5 text-primary" />
                    输入麦克风
                  </p>
                  <p className="mt-0.5 truncate text-[11px] leading-4 text-muted-foreground">
                    {p.audioDevices?.explicit
                      ? `已指定：${p.audioDevices.explicit}`
                      : "跟随系统默认录音设备（手机当麦克风/USB 麦选这里）"}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <select
                    aria-label="选择输入麦克风"
                    value={p.audioDevices?.explicit ?? ""}
                    onChange={(e) => void p.saveAudioDevice(e.target.value)}
                    disabled={!p.audioDevices || p.audioDevices.items.length === 0}
                    className="h-8 max-w-[190px] rounded-md border border-border bg-card px-2 text-xs text-card-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-50"
                  >
                    <option value="">跟随系统默认</option>
                    {p.audioDevices?.items.map((d) => (
                      <option key={d.name} value={d.name}>
                        {d.name}
                        {d.is_default ? "（默认）" : ""}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => void p.refreshAudioDevices()}
                    aria-label="刷新设备列表（插上新麦克风后点这里）"
                    className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-card text-muted-foreground transition hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                  >
                    <RefreshCw className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>

              <div className="flex items-center justify-between border-t border-border/60 pt-3">
                <div className="min-w-0">
                  <p className="text-xs font-medium text-card-foreground">输入降噪</p>
                  <p className="text-[11px] leading-4 text-muted-foreground">
                    压掉环境噪声再变声（约 +40ms 延迟）
                  </p>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={p.audioDevices?.denoise ?? true}
                  onClick={() => void p.toggleDenoise(!(p.audioDevices?.denoise ?? true))}
                  className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${p.audioDevices?.denoise ? "bg-primary" : "bg-muted-foreground/30"}`}
                >
                  <span
                    className={`absolute top-0.5 h-4 w-4 rounded-full bg-background shadow transition-all ${p.audioDevices?.denoise ? "left-[18px]" : "left-0.5"}`}
                  />
                </button>
              </div>
            </div>

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
                变声在后台运行（无窗口）。点「停止」会自动还原声卡；若异常退出，随时到左侧栏点「一键恢复音频」。
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

        {/* 03.5 效果阶梯：想更像？回去补素材 */}
        <EffectLadderCard
          currentLevel={p.modelOk ? 2 : p.datasetCount > 0 ? 1 : 0}
          modelReady={p.modelOk}
          compact
        />

        {/* 04 训练日志：默认收起成一行，点击展开；出错自动弹出 */}
        {(logLines.length > 0 || trainRunning) && (
          <section>
            <button
              type="button"
              onClick={() => setLogOpen((v) => !v)}
              aria-expanded={logOpen}
              className={cn(
                "flex w-full items-center gap-2 rounded-xl border px-3 py-2.5 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                logError
                  ? "border-destructive/40 bg-destructive/10 text-destructive hover:border-destructive/60"
                  : "border-border bg-card/70 text-card-foreground hover:border-primary/40",
              )}
            >
              <Terminal className="h-3.5 w-3.5 shrink-0" />
              <span>训练日志</span>
              <span className={cn("truncate text-[11px]", logError ? "text-destructive/80" : "text-muted-foreground")}>
                {logSummary}
              </span>
              {trainRunning && !logError && (
                <span className="ml-1 h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-primary" aria-hidden />
              )}
              <ChevronDown className={cn("ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform", logOpen && "rotate-180")} />
            </button>
            {logOpen && <TrainLogConsole lines={logLines} running={trainRunning} className="mt-2" />}
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
