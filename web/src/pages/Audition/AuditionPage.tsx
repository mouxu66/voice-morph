import { useMemo, useState } from "react"
import { Link } from "react-router-dom"
import {
  Check,
  CircleStop,
  Gauge,
  Headphones,
  Loader2,
  Mic,
  Radio,
  RefreshCw,
  Search,
  AudioLines,
  Sparkles,
  Square,
  Upload,
  Volume2,
  Wand2,
  Zap,
} from "lucide-react"
import { mediaUrl } from "@/api/client"
import { PageShell, Section, Card } from "@/components/layout/PageShell"
import { ErrorPanel } from "@/components/ErrorPanel"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { downloadUrl } from "@/lib/download"
import { cn } from "@/lib/utils"
import type { AuditionEnv, AuditionResult, AuditionSource } from "@/api/client"
import { useAudition } from "@/pages/Audition/useAudition"
import type { AuditionFilter, TrialVoice } from "@/pages/Audition/useAudition"

const PITCH_PRESETS: [string, number][] = [
  ["原调", 0],
  ["男→女 +12", 12],
  ["女→男 -12", -12],
]

const FILTERS: [AuditionFilter, string][] = [
  ["all", "全部"],
  ["ready", "能直接试"],
  ["mine", "我的"],
  ["market", "市场的"],
]

/** 环境态势条：谁在占显卡、还剩多少显存 —— 不让用户点下去才收到 409 */
function EnvBar({ env, onRefresh }: { env: AuditionEnv | null; onRefresh: () => void }) {
  if (!env) {
    return (
      <Card className="flex items-center gap-2 px-4 py-3 text-xs text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />正在读取显卡与任务状态…
      </Card>
    )
  }
  const blocked = !!env.busy_reason || env.low_vram
  return (
    <Card
      tone={blocked ? "flat" : "raised"}
      className={cn("flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 text-xs", blocked && "border-yellow-500/40 bg-yellow-500/10")}
    >
      <span className="flex items-center gap-1.5 font-medium">
        <Gauge className={cn("h-3.5 w-3.5", blocked ? "text-yellow-600" : "text-primary")} />
        {env.gpu_free_mb != null ? (
          <span className={blocked ? "text-yellow-700" : "text-foreground"}>
            显存余量 {env.gpu_free_mb} MB
            <span className="ml-1 font-normal text-muted-foreground">（安全线 {env.min_free_vram_mb} MB）</span>
          </span>
        ) : (
          <span className="text-muted-foreground">未检测到可用的 NVIDIA 显卡信息</span>
        )}
      </span>
      {env.live_running && (
        <span className="flex items-center gap-1.5 text-yellow-700">
          <Radio className="h-3.5 w-3.5" />实时变声在跑{env.live_exp ? `（${env.live_exp}）` : ""}
        </span>
      )}
      {env.cascade_running && <span className="text-yellow-700">级联变声在跑</span>}
      {env.offline_running && <span className="text-yellow-700">离线变声任务在跑</span>}
      {env.busy_reason && <span className="text-yellow-700">{env.busy_reason} → 批量试音暂不可用</span>}
      {!blocked && <span className="text-muted-foreground">可以开跑</span>}
      <span className="flex-1" />
      <button
        type="button"
        onClick={onRefresh}
        className="inline-flex items-center gap-1.5 text-muted-foreground transition hover:text-foreground"
      >
        <RefreshCw className="h-3.5 w-3.5" />刷新
      </button>
    </Card>
  )
}

function VoiceAvatar({ v }: { v: TrialVoice }) {
  if (v.image) {
    return (
      <img
        src={mediaUrl(v.image)}
        alt=""
        loading="lazy"
        className="h-9 w-9 shrink-0 rounded-lg border border-border object-cover"
      />
    )
  }
  return (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border bg-muted text-xs font-medium text-muted-foreground">
      {v.name.slice(0, 1)}
    </span>
  )
}

/** 备选音色列表项：批量模式勾选，实时模式点一次换一个 */
function VoiceRow({
  v,
  picked,
  disabled,
  onClick,
}: {
  v: TrialVoice
  picked: boolean
  disabled: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={picked}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition",
        picked ? "border-primary/60 bg-primary/[0.10]" : "border-border bg-background/60 hover:border-input",
        disabled && "pointer-events-none opacity-50",
      )}
    >
      <VoiceAvatar v={v} />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-foreground" title={v.id}>
            {v.name}
          </span>
          {!v.ready && (
            <span className="shrink-0 rounded border border-border px-1 py-0.5 text-[10px] text-muted-foreground">
              需下载
            </span>
          )}
        </span>
        <span className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <span className="truncate font-mono">{v.id}</span>
          {v.hasReference ? (
            <span className="shrink-0 text-primary">可输字</span>
          ) : (
            <span className="shrink-0">只能换音色</span>
          )}
        </span>
      </span>
      {picked && <Check className="h-4 w-4 shrink-0 text-primary" />}
    </button>
  )
}

/** 试音结果卡片：一条音频 + 客观分 + 出口动作 */
function ResultCard({
  r,
  scoring,
  onHandoff,
}: {
  r: AuditionResult
  /** 打分阶段还在跑（此时"没有分数"是"还没算完"，而不是"算不了"） */
  scoring: boolean
  onHandoff: (url: string, voiceId: string) => Promise<boolean>
}) {
  const [dlBusy, setDlBusy] = useState(false)
  const [dlErr, setDlErr] = useState("")
  const [going, setGoing] = useState(false)
  const pendingScore = scoring && r.status === "done" && !r.score_error &&
    r.secs == null && r.nats == null

  const doDownload = async () => {
    setDlErr("")
    setDlBusy(true)
    try {
      await downloadUrl(mediaUrl(r.url), `试音间-${r.voice_id}.wav`)
    } catch (e) {
      setDlErr(e instanceof Error ? e.message : "下载失败")
    } finally {
      setDlBusy(false)
    }
  }

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-foreground" title={r.voice_id}>
            {r.display_name}
          </p>
          <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">{r.voice_id}</p>
        </div>
        {r.status === "running" && (
          <span className="flex shrink-0 items-center gap-1.5 text-xs text-primary">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />试音中
          </span>
        )}
        {r.status === "done" && r.from_cache && (
          <span className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
            复用上次
          </span>
        )}
      </div>

      {r.status === "done" && r.url && (
        <>
          <StudioAudioPlayer src={r.url} label="播放试音结果" />
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="rounded-full bg-muted px-2 py-0.5 text-muted-foreground">
              {r.duration_s != null ? `${r.duration_s}s` : "—"}
            </span>
            {pendingScore ? (
              <span className="flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-muted-foreground">
                <Loader2 className="h-3 w-3 animate-spin" />算分中
              </span>
            ) : (
              <>
                {r.secs != null ? (
                  <span
                    className="rounded-full bg-primary/10 px-2 py-0.5 font-medium text-primary"
                    title="CAM++ 声纹余弦相似度：输出与目标音色参考音的接近程度"
                  >
                    像度 {r.secs}
                  </span>
                ) : (
                  <span
                    className="rounded-full bg-muted px-2 py-0.5 text-muted-foreground"
                    title="该音色没有参考音（市场 RVC 权重），无法计算音色相似度"
                  >
                    像度 不可算
                  </span>
                )}
                {r.nats != null ? (
                  <span
                    className="rounded-full bg-primary/10 px-2 py-0.5 font-medium text-primary"
                    title="NatScore 自然度：越高越像真人说话，可为负"
                  >
                    自然度 {r.nats}
                  </span>
                ) : (
                  <span className="rounded-full bg-muted px-2 py-0.5 text-muted-foreground" title="自然度打分器不可用">
                    自然度 不可算
                  </span>
                )}
              </>
            )}
          </div>
          {r.score_error && <p className="text-[11px] text-yellow-600">打分未完成：{r.score_error}</p>}
          <div className="mt-auto flex flex-wrap items-center gap-3 border-t border-border pt-3 text-xs">
            <button
              type="button"
              disabled={dlBusy}
              onClick={() => void doDownload()}
              className="inline-flex items-center gap-1 font-medium text-primary transition hover:underline disabled:opacity-50"
            >
              {dlBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Volume2 className="h-3.5 w-3.5" />}
              {dlBusy ? "下载中…" : "下载 wav"}
            </button>
            <Link
              to="/offlinevc"
              onClick={async (e) => {
                e.preventDefault()
                setGoing(true)
                const ok = await onHandoff(r.url, r.voice_id)
                setGoing(false)
                if (ok) window.location.hash = "#/offlinevc"
              }}
              className="inline-flex items-center gap-1 font-medium text-muted-foreground transition hover:text-foreground"
            >
              {going ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
              拿去微调
            </Link>
          </div>
          {dlErr && <p className="text-[11px] text-destructive">下载失败：{dlErr}</p>}
        </>
      )}

      {r.status === "failed" && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {r.error || "试音失败"}
        </div>
      )}

      {r.status === "running" && (
        <p className="text-xs text-muted-foreground">整段单次推理，一般十几秒到两三分钟…</p>
      )}
    </Card>
  )
}

/** 试音音频面板：同一段素材喂给所有音色，这样比出来才公平 */
function SourcePanel(p: ReturnType<typeof useAudition>) {
  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-3">
        {p.recording ? (
          <button
            type="button"
            onClick={p.stopRecording}
            className="inline-flex items-center gap-2 rounded-md bg-destructive px-4 py-2.5 text-sm font-medium text-destructive-foreground transition hover:opacity-90"
          >
            <CircleStop className="h-4 w-4" />
            停止录音（{p.recordSeconds}s）
          </button>
        ) : (
          <button
            type="button"
            disabled={p.uploading}
            onClick={() => void p.startRecording()}
            className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          >
            <Mic className="h-4 w-4" />对着麦克风说一句
          </button>
        )}
        <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-3.5 py-2.5 text-sm text-muted-foreground transition hover:border-primary hover:text-primary">
          <Upload className="h-4 w-4" />上传音频
          <input
            type="file"
            accept="audio/*,.wav,.mp3,.m4a,.webm,.flac"
            className="hidden"
            onChange={(e) => {
              p.useFileSource(e.target.files?.[0])
              e.target.value = ""
            }}
          />
        </label>
        <button
          type="button"
          disabled={p.uploading}
          onClick={() => void p.useBuiltinSource()}
          className="inline-flex items-center gap-2 rounded-md border border-border bg-background px-3.5 py-2.5 text-sm text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-50"
        >
          <Zap className="h-4 w-4" />用内置示范片段
        </button>
        {p.uploading && (
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />处理音频中…
          </span>
        )}
      </div>

      {p.source ? (
        <div className="mt-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">当前试音音频</span>
            <span className="font-mono">{p.source.source_id}</span>
            <span>{p.source.duration_s}s</span>
            {p.source.builtin && <span className="rounded border border-border px-1.5 py-0.5">内置示范片段</span>}
          </div>
          <StudioAudioPlayer src={p.source.url} label="听听这段源音频" className="mt-2" />
        </div>
      ) : (
        <p className="mt-4 text-xs text-yellow-600">还没有可用的源音频，先录一段、上传一个文件，或用内置示范片段。</p>
      )}

      {p.sources.length > 1 && (
        <div className="mt-4 border-t border-border pt-3">
          <p className="text-[11px] font-medium text-muted-foreground">用过的音频（点一下换回来）</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {p.sources.map((s: AuditionSource) => (
              <span key={s.source_id} className="inline-flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => p.pickSource(s.source_id)}
                  className={cn(
                    "rounded-full px-2.5 py-1 text-[11px] transition",
                    p.source?.source_id === s.source_id
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted text-muted-foreground hover:text-foreground",
                  )}
                >
                  {s.builtin ? "内置片段" : s.source_id.replace("src_", "录音 ")} · {s.duration_s}s
                </button>
                {!s.builtin && (
                  <button
                    type="button"
                    onClick={() => void p.removeSource(s.source_id)}
                    aria-label="删除这个音频"
                    className="text-muted-foreground transition hover:text-destructive"
                  >
                    ×
                  </button>
                )}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  )
}

/** 实时试音面板：手动轮换，点哪个换哪个（不做自动连跑） */
function LivePanel(p: ReturnType<typeof useAudition>) {
  return (
    <Card className="p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-3">
        <span
          className={cn(
            "inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium",
            p.liveRunning ? "bg-green-500/15 text-green-700" : "bg-muted text-muted-foreground",
          )}
        >
          <span className={cn("h-2 w-2 rounded-full", p.liveRunning ? "bg-green-500" : "bg-muted-foreground/50")} />
          {p.liveRunning ? `正在试音「${p.nameOf(p.liveExp)}」` : "未开麦"}
        </span>
        {p.liveRunning && (
          <>
            <button
              type="button"
              onClick={() => void p.toggleMonitor()}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              <Headphones className="h-3.5 w-3.5" />
              {p.liveMonitorOn ? "自我监听：开" : "自我监听：关"}
            </button>
            <button
              type="button"
              disabled={p.liveBusy === "__stop__"}
              onClick={() => void p.stopLive()}
              className="inline-flex items-center gap-1.5 rounded-md bg-destructive px-3.5 py-2 text-xs font-medium text-destructive-foreground transition hover:opacity-90 disabled:opacity-50"
            >
              {p.liveBusy === "__stop__" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
              停止
            </button>
          </>
        )}
      </div>

      <p className="mt-3 text-xs leading-6 text-muted-foreground">
        左边备选音色里<strong className="font-medium text-foreground">点哪个音色，就换成哪个</strong> —— 一次只挂一个音色，
        换一个要重载模型（约 10 来秒）。戴上耳机对着麦克风说话，就能听到变身后的自己。
      </p>
      {p.batchRunning && (
        <p className="mt-2 text-xs text-yellow-600">批量试音正在跑，等它结束再开实时（两者都要独占显卡）。</p>
      )}
      {p.env && !p.env.live_running && p.env.busy_reason && (
        <p className="mt-2 text-xs text-yellow-600">当前 {p.env.busy_reason}，开实时前请先停掉。</p>
      )}
      {!p.env?.tts_worker && (
        <p className="mt-2 text-xs text-muted-foreground">提示：语音合成引擎未驻留，实时变声本身不受影响。</p>
      )}
    </Card>
  )
}

export function AuditionPage() {
  const p = useAudition()
  const [witMode, setWitMode] = useState<"batch" | "live">("batch")

  const pickedCount = p.selected.length
  const progress = useMemo(() => {
    const total = p.task?.total || 0
    const done = p.task?.finished || 0
    return { total, done, pct: total ? Math.round((done / total) * 100) : 0 }
  }, [p.task?.total, p.task?.finished])

  const voiceListCard = (
    <Card className="flex max-h-[calc(100vh-160px)] flex-col p-4">
      <div className="flex items-center gap-2">
        <AudioLines className="h-4 w-4 text-primary" />
        <h2 className="text-sm font-medium text-foreground">备选音色</h2>
        <span className="text-[11px] text-muted-foreground">
          {p.visibleWardrobe.length}/{p.voiceList.length} 款
        </span>
        <span className="flex-1" />
        {witMode === "batch" && (
          <>
            <button
              type="button"
              onClick={p.selectAllVisible}
              className="text-[11px] text-muted-foreground transition hover:text-foreground"
            >
              全选
            </button>
            {pickedCount > 0 && (
              <button
                type="button"
                onClick={p.clearSelection}
                className="text-[11px] text-muted-foreground transition hover:text-destructive"
              >
                清空
              </button>
            )}
          </>
        )}
      </div>

      <div className="relative mt-3">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <input
          value={p.keyword}
          onChange={(e) => p.setKeyword(e.target.value)}
          placeholder="搜音色名或 ID"
          className="w-full rounded-md border border-border bg-background py-2 pl-8 pr-3 text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary"
        />
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {FILTERS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => p.setFilter(key)}
            className={cn(
              "rounded-full px-2.5 py-1 text-[11px] transition",
              p.filter === key ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {p.voiceListError && (
        <div className="mt-3">
          <ErrorPanel title="音色列表读取失败" detail={p.voiceListError} />
        </div>
      )}

      <div className="mt-3 flex-1 space-y-1.5 overflow-y-auto pr-1">
        {p.visibleWardrobe.map((v) => (
          <VoiceRow
            key={v.id}
            v={v}
            picked={witMode === "batch" ? p.selected.includes(v.id) : p.liveExp === v.id}
            disabled={witMode === "live" ? p.batchRunning || !!p.liveBusy : p.batchRunning}
            onClick={() => {
              if (witMode === "live") void p.startLive(v.id)
              else p.toggleVoice(v.id)
            }}
          />
        ))}
        {!p.visibleWardrobe.length && (
          <p className="py-6 text-center text-xs text-muted-foreground">没有匹配的音色</p>
        )}
      </div>

      <p className="mt-3 border-t border-border pt-2.5 text-[11px] leading-5 text-muted-foreground">
        「需下载」的音色会在试音时自动取权重（与安装共用同一份缓存）；标「只能换音色」的是市场
        RVC 权重，没有参考音，不能用文字合成。
      </p>
    </Card>
  )

  return (
    <div className="min-h-full">
      <PageShell wide>
        <div className="space-y-5">
          <EnvBar env={p.env} onRefresh={() => void p.refreshEnv()} />

          <div className="flex flex-wrap items-center gap-2" role="tablist" aria-label="试音方式">
            {(
              [
                ["batch", "一次试多个", "一个声音 × 多个音色，并排听"],
                ["live", "一个个实时试", "对着麦克风，点哪个换哪个"],
              ] as const
            ).map(([key, label, desc]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={witMode === key}
                onClick={() => setWitMode(key)}
                className={cn(
                  "rounded-lg border px-3.5 py-2 text-left transition",
                  witMode === key
                    ? "border-primary/50 bg-primary/[0.10]"
                    : "border-border bg-background/60 hover:border-input",
                )}
              >
                <span className={cn("block text-[13px] font-medium", witMode === key ? "text-primary" : "text-foreground")}>
                  {label}
                </span>
                <span className="mt-0.5 block text-[11px] text-muted-foreground">{desc}</span>
              </button>
            ))}
          </div>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,330px)_minmax(0,1fr)]">
            <div className="lg:sticky lg:top-24 lg:self-start">{voiceListCard}</div>

            <div className="space-y-5">
              {witMode === "live" ? (
                <>
                  <Section
                    eyebrow="实时试音"
                    title="点哪个换哪个"
                    desc="不用等文件转完，开口就能听见。一次只挂一个音色，切换要重载模型。"
                  >
                    <LivePanel {...p} />
                  </Section>
                  <Section eyebrow="配套" title="想用文字的场合" desc="实时路径只认麦克风；要打字合成请切到「一次试多个」的输字模式。">
                    <Card className="flex flex-wrap items-center gap-3 p-4 text-xs text-muted-foreground">
                      <span>想对整段音频做精调（降噪、语气重铸、Seed-VC 补情绪）？</span>
                      <Link to="/offlinevc" className="font-medium text-primary transition hover:underline">
                        去离线变声页 →
                      </Link>
                      <Link to="/tts" className="font-medium text-primary transition hover:underline">
                        去输字变声页 →
                      </Link>
                    </Card>
                  </Section>
                </>
              ) : (
                <>
                  <Section
                    eyebrow="第一步"
                    title="准备一段试音音频"
                    desc="录一句、上传一段，或用内置示范片段。同一段音频会喂给所有选中的音色，这样比出来才公平。"
                  >
                    <SourcePanel {...p} />
                  </Section>

                  <Section
                    eyebrow="第二步"
                    title="调一下参数"
                    desc="不确定就保持默认。变调只影响音高，检索强度是「有多贴目标音色」。"
                    actions={
                      <button
                        type="button"
                        onClick={() => {
                          p.setPitch(0)
                          p.setIndexRate(0.5)
                        }}
                        className="text-xs text-muted-foreground transition hover:text-foreground"
                      >
                        恢复默认
                      </button>
                    }
                  >
                    <Card className="grid grid-cols-1 gap-5 p-4 sm:grid-cols-2 sm:p-5">
                      <div>
                        <label htmlFor="fit-pitch" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                          <span>变调（半音）</span>
                          <span className="font-mono text-primary">{p.pitch > 0 ? `+${p.pitch}` : p.pitch}</span>
                        </label>
                        <input
                          id="fit-pitch"
                          type="range"
                          min={-12}
                          max={12}
                          step={1}
                          value={p.pitch}
                          onChange={(e) => p.setPitch(Number(e.target.value))}
                          className="mt-2 w-full accent-[var(--primary)]"
                        />
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {PITCH_PRESETS.map(([label, v]) => (
                            <button
                              key={label}
                              type="button"
                              onClick={() => p.setPitch(v)}
                              className={cn(
                                "rounded-full px-2.5 py-1 text-[11px] transition",
                                p.pitch === v ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:text-foreground",
                              )}
                            >
                              {label}
                            </button>
                          ))}
                        </div>
                      </div>
                      <div>
                        <label htmlFor="fit-index" className="flex items-center justify-between text-xs font-medium text-muted-foreground">
                          <span>音色检索强度</span>
                          <span className="font-mono text-primary">{p.indexRate.toFixed(2)}</span>
                        </label>
                        <input
                          id="fit-index"
                          type="range"
                          min={0}
                          max={1}
                          step={0.05}
                          value={p.indexRate}
                          onChange={(e) => p.setIndexRate(Number(e.target.value))}
                          className="mt-2 w-full accent-[var(--primary)]"
                        />
                        <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
                          越高越贴目标音色，过高会有训练腔；0.5 左右通常最自然。
                        </p>
                      </div>
                      <label className="flex cursor-pointer items-start gap-2 border-t border-border pt-4 text-xs text-card-foreground sm:col-span-2">
                        <input
                          type="checkbox"
                          checked={p.score}
                          onChange={(e) => p.setScore(e.target.checked)}
                          className="mt-0.5 h-3.5 w-3.5 accent-[var(--primary)]"
                        />
                        <span>
                          给每个结果打客观分（音色像度 + 自然度）
                          <span className="ml-1 text-muted-foreground">
                            （只有带参考音的音色能算音色像度；打分在结果出来之后单独跑，不影响试音速度。
                            显存紧的时候可以关掉）
                          </span>
                        </span>
                      </label>
                    </Card>
                  </Section>

                  <Section
                    eyebrow="第三步"
                    title="试音台"
                    desc="同一段音频，一次试完所有选中的音色，并排听、按客观分排。"
                    actions={
                      <>
                        <button
                          type="button"
                          disabled={p.batchRunning || !p.env?.text_ready}
                          onClick={() => p.setMode(p.mode === "audio" ? "text" : "audio")}
                          className="rounded-md border border-border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-50"
                        >
                          {p.mode === "audio" ? "改用打字合成" : "改用音频换音色"}
                        </button>
                        {p.batchRunning && (
                          <button
                            type="button"
                            onClick={() => void p.cancel()}
                            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs font-medium text-destructive transition hover:bg-destructive/20"
                          >
                            取消
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={!p.canRun}
                          onClick={() => void p.run()}
                          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:pointer-events-none disabled:opacity-50"
                        >
                          {p.batchRunning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                          {p.batchRunning ? "试音中…" : `开始试音${pickedCount ? `（${pickedCount} 个）` : ""}`}
                        </button>
                      </>
                    }
                  >
                    <div className="space-y-4">
                      {p.mode === "text" && (
                        <Card tone="flat" className="p-3.5">
                          <label htmlFor="fit-text" className="text-xs font-medium text-muted-foreground">
                            要合成的话（只有带参考音的音色能走这条路）
                          </label>
                          <textarea
                            id="fit-text"
                            value={p.text}
                            onChange={(e) => p.setText(e.target.value)}
                            rows={2}
                            maxLength={200}
                            className="mt-2 w-full resize-none rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
                          />
                          <p className="mt-1 text-[11px] text-muted-foreground">
                            {p.text.trim().length}/200 字。标「只能换音色」的音色会明确失败——它们没有参考音，无法克隆。
                          </p>
                        </Card>
                      )}

                      {p.runBlockReason && !p.batchRunning && (
                        <p className="rounded-md border border-border bg-muted/40 px-3 py-2.5 text-xs text-muted-foreground">
                          {p.runBlockReason}
                        </p>
                      )}

                      {p.task && p.task.total > 0 && (
                        <Card className="p-3.5">
                          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                            <span className="text-muted-foreground">
                              {p.batchRunning
                                ? p.task.message || p.task.status
                                : p.scoring
                                  ? `正在算客观分（${p.task.score_finished}/${p.task.score_total}）…`
                                  : p.task.message || p.task.status}
                            </span>
                            <span className="font-mono text-muted-foreground">
                              {p.batchRunning ? `${progress.done}/${progress.total}` : ""}
                            </span>
                          </div>
                          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-primary transition-all"
                              style={{
                                width: `${p.batchRunning ? progress.pct : p.scoring ? 100 : progress.pct}%`,
                              }}
                            />
                          </div>
                          {p.scoring && (
                            <p className="mt-2 text-[11px] text-muted-foreground">
                              推理已经全部结束了，你现在就能听；分数会逐条补上。
                            </p>
                          )}
                        </Card>
                      )}

                      {p.task?.status === "error" && <ErrorPanel title="试音中断" detail={p.task.error} />}

                      {p.results.length > 0 ? (
                        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                          {p.results.map((r) => (
                            <ResultCard key={r.voice_id} r={r} scoring={p.scoring} onHandoff={p.handoffToOfflineVc} />
                          ))}
                        </div>
                      ) : (
                        <Card tone="flat" className="flex flex-col items-center gap-2 px-4 py-10 text-center">
                          <AudioLines className="h-6 w-6 text-muted-foreground" />
                          <p className="text-sm text-muted-foreground">还没试音过。左边挑几个音色，点「开始试音」。</p>
                          <p className="text-[11px] text-muted-foreground">
                            每个大约十几秒到两三分钟；同样的参数第二次点会秒出（复用上次结果）。
                          </p>
                        </Card>
                      )}
                    </div>
                  </Section>
                </>
              )}
            </div>
          </div>
        </div>
      </PageShell>
    </div>
  )
}
