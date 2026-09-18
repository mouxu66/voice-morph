import { useState } from "react"
import {
  AudioWaveform,
  Download,
  Gauge,
  Music4,
  Play,
  Plus,
  Sliders,
  Trash2,
  TrendingUp,
  TrendingDown,
  Wand2,
} from "lucide-react"
import type { useEffects } from "@/pages/Effects/useEffects"
import type { FxMeta, FxStep } from "@/pages/Effects/useEffects"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

/** 效果图标映射：后端目录的 icon 字段 → lucide 组件（新增效果改两处即可） */
const FX_ICONS: Record<string, typeof Music4> = {
  reverb: Music4,
  echo: AudioWaveform,
  eq: Sliders,
  pitch: TrendingUp,
  speed: Gauge,
  telephone: Play,
  robot: Wand2,
  tremolo: AudioWaveform,
  chorus: Music4,
  limiter: TrendingDown,
}

function FxIcon({ icon }: { icon: string }) {
  const C = FX_ICONS[icon] ?? Sliders
  return <C className="h-4 w-4" />
}

/** 单个已添加效果的卡片：排序按钮 + 参数滑块 + 删除 */
function ChainStep({
  step,
  meta,
  index,
  total,
  onMove,
  onRemove,
  onParam,
}: {
  step: FxStep
  meta: FxMeta | undefined
  index: number
  total: number
  onMove: (i: number, dir: -1 | 1) => void
  onRemove: (i: number) => void
  onParam: (i: number, key: string, v: number) => void
}) {
  if (!meta) return null
  return (
    <div className="rounded-xl border border-border bg-card/85 p-4 shadow-md backdrop-blur-xl">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/15 text-primary">
            <FxIcon icon={meta.icon} />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-card-foreground">
              {index + 1}. {meta.name}
            </p>
            <p className="truncate text-[11px] text-muted-foreground">{meta.desc}</p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={() => onMove(index, -1)}
            disabled={index === 0}
            aria-label="上移"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-30"
          >
            <TrendingUp className="h-3.5 w-3.5 rotate-90" />
          </button>
          <button
            type="button"
            onClick={() => onMove(index, 1)}
            disabled={index === total - 1}
            aria-label="下移"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-30"
          >
            <TrendingDown className="h-3.5 w-3.5 rotate-90" />
          </button>
          <button
            type="button"
            onClick={() => onRemove(index)}
            aria-label="移除效果"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground transition hover:border-destructive hover:text-destructive"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      <div className="mt-3 grid gap-2.5 sm:grid-cols-2">
        {meta.params.map((p) => (
          <div key={p.key}>
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-medium text-muted-foreground">{p.label}</span>
              <span className="font-mono text-[11px] text-primary">{step.params[p.key]}</span>
            </div>
            <input
              type="range"
              min={p.min}
              max={p.max}
              step={p.step}
              value={step.params[p.key]}
              onChange={(e) => onParam(index, p.key, Number(e.target.value))}
              className="mt-1 w-full accent-[hsl(var(--primary))]"
            />
          </div>
        ))}
      </div>
    </div>
  )
}

/** 简易播放器：原生 audio + 装饰壳 */
function AudioBox({ label, src, primary }: { label: string; src: string | null; primary?: boolean }) {
  return (
    <div
      className={cn(
        "rounded-xl border p-3.5",
        primary ? "border-primary/40 bg-primary/5" : "border-border bg-card/60",
      )}
    >
      <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">{label}</p>
      {src ? (
        <audio controls src={src} className="mt-2 w-full" />
      ) : (
        <p className="mt-3 text-center text-[11px] text-muted-foreground">待生成</p>
      )}
    </div>
  )
}

export function EffectsPage(p: ReturnType<typeof useEffects>) {
  const [presetName, setPresetName] = useState("")
  const added = new Set(p.chain.map((s) => s.type))

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="text-xs font-medium text-primary">特效工坊</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">
            效果器工坊
          </h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            给任意变声/合成结果加最后一道工序：混响、回声、电话音、机器人、合唱、EQ、变调变速…
            十种本地 DSP 效果自由叠加，CPU 计算、不占显卡、不与实时变声抢资源。
          </p>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-8 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        {/* 01 音频源 */}
        <section>
          <p className="text-xs font-medium text-primary">01 / 音频源</p>
          <div className="mt-3 grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <label className="flex cursor-pointer flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-border bg-card/50 px-6 py-10 text-center transition hover:border-primary/60">
              <AudioWaveform className="h-8 w-8 text-primary" />
              <span className="text-sm font-medium text-card-foreground">
                {p.sourceFile ? p.sourceFile.name : "点击选择音频文件（wav/mp3/flac）"}
              </span>
              <span className="text-xs text-muted-foreground">
                {p.sourceFile
                  ? `${(p.sourceFile.size / 1024).toFixed(0)} KB · 换文件重新选择即可`
                  : "变声/克隆/有声书的输出文件都能加工"}
              </span>
              <input
                type="file"
                accept="audio/*,.wav,.mp3,.flac,.ogg,.m4a"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) p.setSource(f)
                  e.target.value = ""
                }}
              />
            </label>
            <AudioBox label="原始音频" src={p.sourceUrl} />
          </div>
        </section>

        {/* 02 效果链 */}
        <section>
          <div className="flex items-end justify-between gap-3">
            <div>
              <p className="text-xs font-medium text-primary">02 / 效果链</p>
              <h3 className="mt-1 text-lg font-semibold text-card-foreground">按顺序叠加效果</h3>
            </div>
            {p.chain.length > 0 && (
              <button
                type="button"
                onClick={p.clearChain}
                className="text-xs text-muted-foreground transition hover:text-destructive"
              >
                清空链
              </button>
            )}
          </div>

          <div className="mt-3 space-y-3">
            {p.chain.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-border bg-card/40 px-6 py-8 text-center">
                <p className="text-sm text-muted-foreground">效果链为空——从下方效果库添加效果，处理将直通原音频。</p>
              </div>
            ) : (
              p.chain.map((step, i) => {
                const meta = p.catalog.find((m) => m.type === step.type)
                return (
                  <ChainStep
                    key={`${step.type}-${i}`}
                    step={step}
                    meta={meta}
                    index={i}
                    total={p.chain.length}
                    onMove={p.moveFx}
                    onRemove={p.removeFx}
                    onParam={p.setParam}
                  />
                )
              })
            )}
          </div>

          {/* 效果库 */}
          <div className="mt-5">
            <p className="text-xs font-medium text-card-foreground">效果库（点击加入链尾）</p>
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
              {p.catalog.map((m) => {
                const used = added.has(m.type)
                return (
                  <button
                    key={m.type}
                    type="button"
                    onClick={() => p.addFx(m)}
                    disabled={used}
                    className={cn(
                      "flex flex-col items-start gap-2 rounded-xl border p-3 text-left transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                      used
                        ? "border-border bg-muted/40 opacity-50"
                        : "border-border bg-card hover:border-primary/50 hover:shadow-md",
                    )}
                  >
                    <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/15 text-primary">
                      <FxIcon icon={m.icon} />
                    </span>
                    <span className="text-xs font-semibold text-card-foreground">{m.name}</span>
                    <span className="text-[10px] leading-4 text-muted-foreground">{m.desc}</span>
                    {used ? (
                      <span className="text-[10px] text-muted-foreground">已在链中</span>
                    ) : (
                      <span className="flex items-center gap-1 text-[10px] text-primary">
                        <Plus className="h-3 w-3" />添加
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
        </section>

        {/* 03 应用与对比 */}
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-xs font-medium text-primary">03 / 应用与对比</p>
              <h3 className="mt-1 text-lg font-semibold text-card-foreground">生成并试听</h3>
            </div>
            <button
              type="button"
              onClick={p.apply}
              disabled={!p.sourceFile || p.processing}
              className="btn-primary"
            >
              {p.processing ? "处理中…" : p.chain.length ? `应用 ${p.chain.length} 个效果` : "直通（未加效果）"}
            </button>
          </div>

          <div className="mt-4 grid gap-3 lg:grid-cols-2">
            <AudioBox label="原始" src={p.sourceUrl} />
            <AudioBox label="处理后" src={p.resultUrl} primary />
          </div>

          {p.resultUrl && (
            <a
              href={p.resultUrl}
              download={`fx_${p.sourceFile?.name ?? "output"}.wav`}
              className="mt-3 inline-flex items-center gap-2 rounded-xl border border-primary/40 bg-primary/10 px-4 py-2 text-sm font-medium text-primary transition hover:bg-primary/20"
            >
              <Download className="h-4 w-4" />
              下载处理结果（wav）
            </a>
          )}

          {p.feedback && (
            p.feedback.tone === "error"
              ? <ErrorPanel title="效果应用失败" detail={p.feedback.text} />
              : <p className={cn("mt-4 rounded-xl border px-4 py-3 text-xs leading-5",
                  p.feedback.tone === "ok" && "border-emerald-500/30 bg-emerald-500/10 text-emerald-600",
                  p.feedback.tone === "info" && "border-border bg-card/70 text-card-foreground")}>{p.feedback.text}</p>
          )}
        </section>

        {/* 04 预设 */}
        <section>
          <p className="text-xs font-medium text-primary">04 / 效果预设</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={presetName}
              onChange={(e) => setPresetName(e.target.value)}
              placeholder="预设名（如：教堂空旷感）"
              className="w-48 rounded-lg border border-border bg-background/60 px-3 py-2 text-xs text-card-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none"
            />
            <button
              type="button"
              onClick={() => {
                p.savePreset(presetName)
                setPresetName("")
              }}
              disabled={!presetName.trim() || p.chain.length === 0}
              className="btn-ghost"
            >
              保存当前链
            </button>
          </div>
          {p.presets.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {p.presets.map((preset) => (
                <span
                  key={preset.name}
                  className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1.5 text-xs text-card-foreground"
                >
                  <button type="button" onClick={() => p.loadPreset(preset.name)} className="transition hover:text-primary">
                    {preset.name}（{preset.chain.length} 效果）
                  </button>
                  <button
                    type="button"
                    onClick={() => p.removePreset(preset.name)}
                    aria-label="删除预设"
                    className="text-muted-foreground transition hover:text-destructive"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
