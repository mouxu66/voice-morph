import { CheckCircle2, CircleAlert, Loader2, Mic2, MessageSquareText, Play, RefreshCw, Repeat2, Square, Sparkles } from "lucide-react"
import type { ReactNode } from "react"
import type { useKangaroo } from "@/pages/Kangaroo/useKangaroo"

function StatusBadge({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-600" : "border-muted bg-muted/40 text-muted-foreground"}`}>
      {ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <CircleAlert className="h-3.5 w-3.5" />}
      {children}
    </span>
  )
}

export function KangarooPage(p: ReturnType<typeof useKangaroo>) {
  const disabled = !p.modelOk && !p.liveOn
  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-10 sm:px-8 lg:px-12 lg:py-14">
        <div className="mx-auto max-w-7xl">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">ONE &middot; TAP / KANGAROO</p>
          <h2 className="mt-4 font-display text-4xl font-bold tracking-tight text-foreground sm:text-5xl">袋鼠语音</h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-muted-foreground">
            三步走：<span className="text-foreground">打字</span> → <span className="text-foreground">变声</span> → <span className="text-foreground">训练</span>。打开变声工坊，点一下「实时变袋鼠音」，打开微信直接说。
          </p>
          <div className="mt-5 flex flex-wrap items-center gap-2">
            <StatusBadge ok={p.modelOk}>袋鼠模型{p.modelOk ? "已就绪" : "未就绪"}</StatusBadge>
            <StatusBadge ok={p.datasetCount > 0}>{p.datasetCount} 条语料</StatusBadge>
            <StatusBadge ok={p.liveOn}>实时{p.liveOn ? "运行中" : "未启动"}</StatusBadge>
            <StatusBadge ok={p.trainingRunning}>训练{p.trainingRunning ? "进行中" : "空闲"}</StatusBadge>
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl gap-10 px-5 py-10 sm:px-8 lg:grid-cols-3 lg:px-12 lg:py-14">
        {/* ① 打字 → 袋鼠语音 */}
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">01 / 打字</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <MessageSquareText className="h-4 w-4 text-primary" />打字 → 袋鼠语音
          </h3>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">输入一句想说的话，让袋鼠用它的语调念出来，可试听、可下载。</p>
          <textarea
            value={p.script}
            onChange={(event) => p.setScript(event.target.value)}
            rows={4}
            placeholder="比如：老板，我要两个烤串，再来一瓶可乐"
            className="mt-5 w-full resize-none rounded-md border border-border bg-background px-3 py-2.5 text-sm text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
          />
          <button
            type="button"
            disabled={!p.script.trim()}
            onClick={p.toTts}
            className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
          >
            <Sparkles className="h-4 w-4" />去打字生成语音
          </button>
        </section>

        {/* ② 实时变袋鼠音 */}
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">02 / 变声</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <Repeat2 className="h-4 w-4 text-primary" />实时变袋鼠音
          </h3>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            一键启动 RVC 实时变声：自动把录音设备切到虚拟声卡（{p.liveStatus?.output_device ?? "CABLE Output"}），退出或停止后自动还原你的设备设置。
          </p>
          {p.liveOn ? (
            <button
              type="button"
              onClick={p.stop}
              disabled={p.stopping}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-md bg-destructive px-4 py-3 text-sm font-medium text-destructive-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-60"
            >
              {p.stopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
              {p.stopping ? "正在停止并还原声卡…" : "停止变袋鼠音"}
            </button>
          ) : (
            <button
              type="button"
              onClick={p.start}
              disabled={p.starting || disabled}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-3 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
            >
              {p.starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {p.starting ? "正在启动…" : "实时变袋鼠音"}
            </button>
          )}
          {p.liveOn && (
            <p className="mt-3 flex items-center gap-2 text-xs text-primary">
              <CircleAlert className="h-3.5 w-3.5 shrink-0" />实时变声中：关闭 RVC 窗口或点「停止」会自动还原设备；若忘了关，随时在左侧栏点「一键恢复音频」。
            </p>
          )}
          {!p.modelOk && (
            <p className="mt-3 flex items-center gap-2 text-xs text-destructive">
              <CircleAlert className="h-3.5 w-3.5" />袋鼠模型未就绪，请先点「训练袋鼠模型」。
            </p>
          )}
        </section>

        {/* ③ 训练袋鼠模型 */}
        <section className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
          <p className="font-mono text-xs uppercase tracking-widest text-primary">03 / 训练</p>
          <h3 className="mt-2 flex items-center gap-2 text-lg font-semibold text-card-foreground">
            <Mic2 className="h-4 w-4 text-primary" />训练袋鼠模型
          </h3>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            自动跑完预处理 → 提音高 → 提特征 → 训练 → 生成索引。全程后台进行，模型就绪后即可实时变声。
          </p>
          {p.trainingRunning ? (
            <div className="mt-5 flex w-full items-center justify-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-4 py-3 text-sm font-medium text-primary">
              <RefreshCw className="h-4 w-4 animate-spin" />训练中，请在新窗口查看进度…
            </div>
          ) : (
            <button
              type="button"
              onClick={p.train}
              disabled={p.training}
              className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-4 py-3 text-sm font-medium text-primary transition hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
            >
              {p.training ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic2 className="h-4 w-4" />}
              {p.modelOk ? "模型已就绪 · 重新训练" : "训练袋鼠模型"}
            </button>
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