import { useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  CloudDownload,
  FileAudio,
  FolderOpen,
  Headphones,
  Loader2,
  RefreshCw,
  RotateCcw,
  Search,
  ScrollText,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  X,
  XCircle,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { mediaUrl } from "@/api/client"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { ErrorPanel } from "@/components/ErrorPanel"
import type { MarketFile, MarketItem } from "@/api/client"
import type { VoiceMarket, MarketPlatformFilter } from "@/pages/VoiceMarket/useVoiceMarket"
import { ACTIVE_PHASE_TEXT, fmtBytes, pctOf } from "@/pages/VoiceMarket/marketFormat"

const PLATFORM_LABEL: Record<string, string> = { hf: "HF", modelscope: "魔搭" }

type SortKey = "relevance" | "downloads" | "updated"
const SORT_OPTIONS: ReadonlyArray<[SortKey, string]> = [
  ["relevance", "相关"],
  ["downloads", "热度"],
  ["updated", "最新"],
]

// ---------- 全局安装托盘：完成提示 + 当前任务 + 等待队列（可单独移除） ----------
export function MarketInstallBar(p: Pick<VoiceMarket, "task" | "installRunning" | "installErr" | "setInstallErr" | "cancelInstall" | "installQueue" | "justFinished">) {
  const queued = p.installQueue
  if (!p.installRunning && !p.installErr && queued.length === 0 && !p.justFinished) return null
  const t = p.task
  const install = t?.install
  const name = install?.display_name ?? t?.name ?? ""
  const indeterminate = !!t && !t.total && !install?.percent
  const done = (t?.done ?? 0) > 0 && !!t?.total ? fmtBytes(t!.done!) + " / " + fmtBytes(t!.total!) : ""
  const hasTop = p.installRunning || p.installErr || !!p.justFinished

  return (
    <div className="fixed inset-x-0 bottom-4 z-40 flex justify-center px-4">
      <div className="w-full max-w-2xl rounded-2xl border border-border bg-card/95 p-4 shadow-2xl backdrop-blur-xl">
        {p.installErr && (
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-2 text-sm font-medium text-destructive">
                <XCircle className="h-4 w-4 shrink-0" />安装失败
              </p>
              <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{p.installErr}</p>
            </div>
            <button type="button" onClick={() => p.setInstallErr("")} className="rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground" aria-label="关闭">
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        {p.justFinished && (
          <div className={cn("flex items-center gap-3", p.installErr && "mt-3 border-t border-border pt-3")}>
            <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-500" />
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-2 text-sm font-medium text-card-foreground">
                安装完成
                <span className="shrink-0 rounded-full bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] text-emerald-500">已加入音色库</span>
              </p>
              <p className="mt-0.5 truncate text-xs text-muted-foreground" title={p.justFinished.name}>
                「{p.justFinished.name}」可直接用于实时变声与离线工坊
              </p>
            </div>
          </div>
        )}

        {p.installRunning && (
          <div className={cn("flex items-center gap-4", (p.installErr || p.justFinished) && "mt-3 border-t border-border pt-3")}>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <CloudDownload className="h-4 w-4 shrink-0 text-primary" />
                <p className="truncate text-sm font-medium text-card-foreground">{name || "音色安装中"}</p>
                <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">
                  {ACTIVE_PHASE_TEXT(install?.phase ?? t?.status ?? "")}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                  <div
                    className={cn("h-full rounded-full bg-primary", indeterminate && "w-1/3 animate-pulse")}
                    style={indeterminate ? undefined : { width: `${pctOf(t)}%` }}
                  />
                </div>
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                  {indeterminate ? "连接中…" : `${Math.round(pctOf(t))}%${done ? ` · ${done}` : ""}`}
                </span>
              </div>
              {install?.message && <p className="mt-1 truncate text-[11px] text-muted-foreground">{install.message}</p>}
            </div>
            <button
              type="button"
              onClick={() => void p.cancelInstall()}
              className="shrink-0 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground transition hover:border-destructive/40 hover:text-destructive"
            >
              取消
            </button>
          </div>
        )}

        {queued.length > 0 && (
          <div className={cn("mt-3", hasTop && "border-t border-border pt-3")}>
            <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
              等待队列 · {queued.length} 个
            </p>
            <ul className="mt-1.5 space-y-1">
              {queued.map((q) => (
                <li key={q.voice_id} className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
                  <span className="min-w-0 flex-1 truncate" title={q.display_name || q.voice_id}>{q.display_name || q.voice_id}</span>
                  <button
                    type="button"
                    onClick={() => void p.cancelInstall(q.voice_id)}
                    className="shrink-0 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition hover:text-destructive"
                    aria-label={`移除 ${q.display_name || q.voice_id}`}
                  >
                    移除
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}

// ---------- 市场主页：精选与搜索共用同一列表，搜索框常驻顶部 ----------
export function MarketPage(p: VoiceMarket) {
  const { doSearch, clearSearch, platform, setPlatform } = p
  const [localQuery, setLocalQuery] = useState(p.searchQuery)
  const [sort, setSort] = useState<SortKey>("relevance")
  const hasQuery = localQuery.trim().length > 0

  // 输入即搜：停手 400ms 自动搜；清空则回到精选
  useEffect(() => {
    const q = localQuery.trim()
    if (!q) {
      clearSearch()
      return
    }
    const t = window.setTimeout(() => void doSearch(q, platform), 400)
    return () => window.clearTimeout(t)
  }, [localQuery, platform, doSearch, clearSearch])

  const clear = () => {
    setLocalQuery("")
    clearSearch()
  }

  // 排序：HF 有下载量/更新时间；魔搭多为精选精确匹配，缺字段时保持原序
  const sorted = useMemo(() => {
    if (!p.results) return null
    if (sort === "relevance") return p.results
    const arr = [...p.results]
    if (sort === "downloads") arr.sort((a, b) => (b.downloads ?? b.likes ?? 0) - (a.downloads ?? a.likes ?? 0))
    else arr.sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))
    return arr
  }, [p.results, sort])

  const resultCount = sorted?.length ?? 0
  const statusText = p.searching
    ? "搜索中…"
    : hasQuery
      ? sorted === null
        ? "输入关键词开始搜索"
        : resultCount === 0
          ? "没有找到相关仓库"
          : `找到 ${resultCount} 个仓库`
      : `精选 ${p.manifest?.length ?? 0} 款优质音色`

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="sticky top-0 z-30 border-b border-border bg-card/80 px-5 py-4 backdrop-blur-xl sm:px-8 lg:px-12">
        <div className="mx-auto max-w-7xl">
          {/* 第一行：大搜索框 + 大蓝色「搜索」按钮（HMCL 风格） */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <input
                value={localQuery}
                onChange={(e) => setLocalQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void doSearch(localQuery, platform)}
                placeholder="搜索音色、作者或关键词（魔搭支持 owner/name 精确路径）"
                className="h-11 w-full rounded-md border border-border bg-background pl-10 pr-9 text-sm text-foreground shadow-md outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
              />
              {localQuery && (
                <button
                  type="button"
                  onClick={clear}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
                  aria-label="清空搜索"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={() => void doSearch(localQuery, platform)}
              disabled={p.searching || !localQuery.trim()}
              className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-md bg-blue-600 px-7 text-sm font-semibold text-white shadow-md transition hover:bg-blue-500 active:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 disabled:pointer-events-none disabled:opacity-50"
            >
              {p.searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              {p.searching ? "搜索中…" : "搜索"}
            </button>
          </div>

          {/* 第二行：来源 / 排序 下拉（HMCL 风格 select）+ 状态行 */}
          <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
            <label className="flex items-center gap-1.5">
              <span className="text-muted-foreground">来源</span>
              <select
                value={platform}
                onChange={(e) => setPlatform(e.target.value as MarketPlatformFilter)}
                className="h-7 rounded-md border border-border bg-background px-2 text-xs text-foreground shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-primary"
              >
                <option value="all">全部</option>
                <option value="hf">HuggingFace</option>
                <option value="modelscope">魔搭</option>
              </select>
            </label>
            {hasQuery && !p.searching && resultCount > 1 && (
              <label className="flex items-center gap-1.5">
                <span className="text-muted-foreground">排序</span>
                <select
                  value={sort}
                  onChange={(e) => setSort(e.target.value as SortKey)}
                  className="h-7 rounded-md border border-border bg-background px-2 text-xs text-foreground shadow-sm outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  {SORT_OPTIONS.map(([k, label]) => (
                    <option key={k} value={k}>{label}</option>
                  ))}
                </select>
              </label>
            )}

            <span className="ml-auto flex items-center gap-1.5">
              {p.searching ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />搜索中…
                </>
              ) : (
                <span>{statusText}</span>
              )}
              {hasQuery && !p.searching && (
                <button type="button" onClick={clear} className="text-primary transition hover:underline">
                  清除搜索，回到精选
                </button>
              )}
              {!hasQuery && <span className="hidden sm:inline">· 双源直链一键安装到音色库</span>}
            </span>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-5 py-6 sm:px-8 lg:px-12 lg:py-8">
        {p.searchNote && (
          <div className="mb-5 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs leading-5 text-amber-400">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{p.searchNote}</span>
          </div>
        )}

        {hasQuery ? (
          p.results === null || p.searching ? (
            <div className="flex items-center gap-2 rounded-2xl border border-border bg-card/80 px-4 py-6 text-sm text-muted-foreground shadow-md backdrop-blur-xl">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />正在搜索…
            </div>
          ) : resultCount === 0 ? (
            <div className="rounded-2xl border border-border bg-card/80 px-4 py-10 text-center text-sm text-muted-foreground shadow-md backdrop-blur-xl">
              没有找到相关仓库，换个关键词试试。
            </div>
          ) : (
            <>
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {sorted!.map((item) => (
                  <SearchResultCard key={item.id} item={item} p={p} />
                ))}
              </div>
              {p.nextSkip != null && (
                <div className="mt-6 flex flex-col items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => void p.loadMore()}
                    disabled={p.loadingMore}
                    className="inline-flex items-center gap-2 rounded-md border border-border bg-card/85 px-5 py-2.5 text-sm font-medium text-foreground shadow-md transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    {p.loadingMore ? <Loader2 className="h-4 w-4 animate-spin text-primary" /> : <RefreshCw className="h-4 w-4" />}
                    {p.loadingMore ? "加载中…" : "加载更多"}
                  </button>
                  <span className="text-[11px] text-muted-foreground">已加载 {resultCount} 个 · 数据源最多可翻 200 条</span>
                </div>
              )}
            </>
          )
        ) : p.manifest === null ? (
          <div className="flex items-center gap-2 rounded-2xl border border-border bg-card/80 px-4 py-6 text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />正在加载精选清单…
          </div>
        ) : p.manifest.length === 0 ? (
          <div className="rounded-2xl border border-border bg-card/80 px-4 py-8 text-center text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            精选清单为空（后端未启动或清单未配置）。
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {p.manifest.map((item) => (
              <MarketCard key={item.id} item={item} p={p} />
            ))}
          </div>
        )}

        {p.repoOpen && <RepoFilePanel p={p} />}

        <div className="mt-6 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs leading-5 text-amber-400">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            社区自训音色多为「仅供个人学习研究」用途，请遵守对应许可证并勿商用；涉及卡通/真人 IP 的音色另存法律风险，本工具仅提供下载通道，不承担用途责任。
          </p>
        </div>
      </main>
    </div>
  )
}

// HMCL 风格卡片：左侧大缩略图 + 右侧标签/标题/简介/底部操作；两种数据源共用外壳。
// 缩略图优先用 manifest 配图（item.image），无图按分类着色 + 首字母占位。

/** 未安装音色的试听槽（先试听后安装）：ready 出播放器，其余态出可点按钮。
 *  首次试听需先下载模型到市场缓存（约 sizeHintMb），该缓存安装时直接复用。 */
function PreviewSlot({ voiceId, p, onTrigger, sizeHintMb }: {
  voiceId: string
  p: VoiceMarket
  onTrigger: () => void
  sizeHintMb?: number
}) {
  const prev = p.previews[voiceId]
  if (prev?.status === "ready") {
    return <StudioAudioPlayer src={mediaUrl(prev.url)} label="试听" className="min-w-0 flex-1" />
  }
  if (prev?.status === "generating") {
    return (
      <span
        className="flex min-w-0 flex-1 items-center gap-1.5 text-[11px] text-muted-foreground"
        title="首次试听需先下载模型并转换，完成后一键安装可免重复下载"
      >
        <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />试听准备中…
      </span>
    )
  }
  if (prev?.status === "failed" || prev?.status === "skipped") {
    return (
      <button
        type="button"
        onClick={onTrigger}
        title={prev.error || "重试生成试听"}
        className="inline-flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
      >
        <RefreshCw className="h-3 w-3 shrink-0" />重试试听
      </button>
    )
  }
  return (
    <button
      type="button"
      onClick={onTrigger}
      title={`先试听再决定是否安装${sizeHintMb ? `（首次需下载模型约 ${sizeHintMb}M）` : "（首次需下载模型）"}；下载过的模型安装时直接复用`}
      className="inline-flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
    >
      <Headphones className="h-3 w-3 shrink-0" />先试听
    </button>
  )
}
const CATEGORY_TONE: Array<[RegExp, string, string]> = [
  [/卡通|角色/, "from-amber-500/25 to-amber-500/5", "ring-amber-500/30"],
  [/女声/, "from-pink-500/25 to-pink-500/5", "ring-pink-500/30"],
  [/男声/, "from-sky-500/25 to-sky-500/5", "ring-sky-500/30"],
]
const DEFAULT_TONE = ["from-violet-500/25 to-violet-500/5", "ring-violet-500/30"] as const

function MarketThumb({ item }: { item: MarketItem }) {
  const tone = CATEGORY_TONE.find(([re]) => re.test(item.category ?? "")) ?? null
  const [grad, ring] = tone ?? DEFAULT_TONE
  const Initial = (item.name || item.repo || "?").trim().charAt(0).toUpperCase()
  return (
    <div className={cn("relative h-20 w-20 shrink-0 overflow-hidden rounded-xl bg-gradient-to-br ring-1", grad, ring)}>
      {item.image ? (
        <img src={item.image} alt="" loading="lazy" className="h-full w-full object-cover" />
      ) : (
        <span className="flex h-full w-full items-center justify-center font-display text-2xl font-semibold text-foreground/80">
          {Initial}
        </span>
      )}
      <span className="absolute bottom-1 right-1 rounded-md bg-background/85 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground shadow-sm">
        {PLATFORM_LABEL[item.platform] ?? item.platform}
      </span>
    </div>
  )
}

function MarketCard({ item, p }: { item: MarketItem; p: VoiceMarket }) {
  const voiceId = item.voice_id ?? ""
  const isInstalled = !!voiceId && p.installed.includes(voiceId)
  const isThis = p.installRunning && p.installingId === voiceId
  const isQueued = p.queuedIds.includes(voiceId)
  const installPct = isThis ? pctOf(p.task) : 0
  const playable = p.isPlayable(item.demo)
  const prev = p.previews[voiceId]

  // A2：装了但没有仓库演示音频 → 自动生成固定句试听（ready 后底部出播放器）
  useEffect(() => {
    if (isInstalled && !playable) void p.ensurePreview(voiceId)
  }, [isInstalled, playable, voiceId, p.ensurePreview])

  const previewBusy = prev?.status === "generating" || prev?.status === "missing"
  const previewErr = prev?.status === "failed" || prev?.status === "skipped"

  return (
    <article className="flex gap-4 rounded-2xl border border-border bg-card/85 p-4 shadow-md backdrop-blur-xl transition hover:shadow-lg">
      <MarketThumb item={item} />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">{item.category ?? "音色"}</span>
          {item.size_hint_mb != null && (
            <span className="font-mono text-[10px] text-muted-foreground">{fmtBytes(item.size_hint_mb * 1024 * 1024)}</span>
          )}
          {isInstalled && (
            <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 font-mono text-[10px] font-medium text-emerald-400">
              <CheckCircle2 className="h-3 w-3" />已安装
            </span>
          )}
        </div>
        <h3 className="truncate text-base font-semibold text-card-foreground" title={item.name}>{item.name}</h3>
        <p className="line-clamp-2 min-h-[2.5rem] text-xs leading-5 text-muted-foreground">{item.desc}</p>
        {item.license && (
          <p className="rounded-md border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-[11px] leading-4 text-amber-400/90">
            <ShieldCheck className="mr-1 inline h-3 w-3 translate-y-[-1px]" />{item.license}
          </p>
        )}
        <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
          {playable && <StudioAudioPlayer src={item.demo!} label="试听" className="flex-1 min-w-0" />}
          {!playable && prev?.status === "ready" && (
            <StudioAudioPlayer src={mediaUrl(prev.url)} label="试听" className="flex-1 min-w-0" />
          )}
          {!playable && previewBusy && isInstalled && (
            <span className="flex min-w-0 flex-1 items-center gap-1.5 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />试听生成中…
            </span>
          )}
          {!playable && previewErr && isInstalled && (
            <button
              type="button"
              onClick={() => void p.ensurePreview(voiceId, true)}
              title={prev.error}
              className="inline-flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[11px] text-muted-foreground transition hover:border-primary hover:text-primary"
            >
              <RefreshCw className="h-3 w-3 shrink-0" />重新生成试听
            </button>
          )}
          {!playable && !isInstalled && voiceId && prev?.status !== "ready" && (
            <PreviewSlot voiceId={voiceId} p={p} onTrigger={() => p.previewItem(item)} sizeHintMb={item.size_hint_mb} />
          )}
          <div className="ml-auto shrink-0">
            {isInstalled ? (
              <div className="flex items-center gap-1.5">
                {p.backups.includes(voiceId) && (
                  <button
                    type="button"
                    disabled={p.installRunning || p.rollbackingId === voiceId}
                    onClick={() => {
                      if (!window.confirm(`确定将「${item.name}」回滚到覆盖前的旧版本吗？\n将恢复 .old 备份中的模型，并清除当前版本的试听/质检记录。`)) return
                      void p.rollbackVoice(voiceId)
                        .then(() => void p.ensurePreview(voiceId, true))
                        .catch(() => {})
                    }}
                    title="覆盖重装前会自动归档旧版本，安装失败也会自动回滚。此按钮手动恢复上次覆盖前的版本。"
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-primary hover:text-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    {p.rollbackingId === voiceId ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />}
                    回滚
                  </button>
                )}
                <button
                  type="button"
                  disabled={p.installRunning || p.uninstallingId === voiceId}
                  onClick={() => {
                    if (!window.confirm(`确定从音色库卸载「${item.name}」吗？\n将删除其模型（logs/${voiceId}）、权重（assets/weights）与下载缓存，不可恢复。`)) return
                    void p.uninstallVoice(voiceId).catch(() => {})
                  }}
                  className="inline-flex items-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-1.5 text-xs font-medium text-destructive transition hover:bg-destructive/20 disabled:pointer-events-none disabled:opacity-50"
                >
                  {p.uninstallingId === voiceId ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                  卸载
                </button>
              </div>
            ) : isThis ? (
              <div className="flex items-center gap-2">
                <div className="flex h-8 items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-2.5">
                  <div className="h-1 w-16 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full bg-primary" style={{ width: `${installPct}%` }} />
                  </div>
                  <span className="font-mono text-[10px] text-primary">{Math.round(installPct)}%</span>
                </div>
              </div>
            ) : isQueued ? (
              <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs font-medium text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />排队中
              </span>
            ) : (
              <button
                type="button"
                onClick={() =>
                  void p.startInstall(voiceId, item.download!, {
                    index: item.index ?? null,
                    display_name: item.name,
                    manifest_id: item.id,
                  })
                }
                className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                title="有任务进行中时会加入等待队列，完成后自动开始"
              >
                <CloudDownload className="h-3.5 w-3.5" />
                一键安装
              </button>
            )}
          </div>
        </div>
      </div>
    </article>
  )
}

// （原「搜索 Tab」已合并进 MarketPage：搜索框常驻顶部，结果与精选共用同一列表）

function SearchResultCard({ item, p }: { item: MarketItem; p: VoiceMarket }) {
  const prefs = item.prefs
  const quick = p.quickSlot(item)
  const canQuick = !!quick
  const quickVoiceId = quick ? p.deriveVoiceId(quick.download.name, item.repo) : ""
  const isOpen = p.repoOpen?.id === item.id

  return (
    <article className="flex gap-4 rounded-2xl border border-border bg-card/85 p-4 shadow-md backdrop-blur-xl transition hover:shadow-lg">
      <MarketThumb item={item} />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-1.5">
          {(item.tags_zh ?? item.tags ?? []).slice(0, 3).map((t) => (
            <span key={t} className="rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">{t}</span>
          ))}
          {item.downloads != null && item.downloads > 0 && (
            <span className="font-mono text-[10px] text-muted-foreground">下载 {item.downloads.toLocaleString()}</span>
          )}
          {item.updated_at && (
            <span className="font-mono text-[10px] text-muted-foreground">更新 {item.updated_at}</span>
          )}
        </div>
        <h3 className="truncate text-base font-semibold text-card-foreground" title={item.name}>{item.name}</h3>
        <p className="truncate font-mono text-[11px] text-muted-foreground" title={item.repo}>{item.repo}</p>
        {item.desc && <p className="line-clamp-2 min-h-[2.5rem] text-xs leading-5 text-muted-foreground">{item.desc}</p>}
        <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
          <button
            type="button"
            onClick={() => void p.openRepo(item)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition",
              isOpen
                ? "border-primary/50 bg-primary/10 text-primary"
                : "border-border text-muted-foreground hover:border-primary hover:text-primary",
            )}
          >
            <FolderOpen className="h-3.5 w-3.5" />
            {isOpen ? "收起" : "查看文件"}
            {(item.files ?? []).length > 0 && <span className="ml-1 font-mono text-[10px] text-muted-foreground">({item.files!.length})</span>}
          </button>
          {(() => {
            const pvVoice = prefs?.voice_id ?? (canQuick ? quickVoiceId : "")
            if (!pvVoice || p.previews[pvVoice]?.status === "ready") return null
            return <PreviewSlot voiceId={pvVoice} p={p} onTrigger={() => p.previewItem(item)} sizeHintMb={prefs?.size_hint_mb} />
          })()}
          <div className="ml-auto shrink-0">
            {prefs?.download ? (
              <button
                type="button"
                onClick={() =>
                  void p.startInstall(prefs.voice_id ?? item.voice_id ?? item.id, prefs.download!, {
                    index: prefs.index ?? null,
                    display_name: prefs.name ?? item.name,
                    manifest_id: prefs.id,
                  })
                }
                className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                title="有任务进行中时会加入等待队列，完成后自动开始"
              >
                <CloudDownload className="h-3.5 w-3.5" />安装
              </button>
            ) : canQuick ? (
              <button
                type="button"
                onClick={() =>
                  void p.startInstall(quickVoiceId, {
                    url: quick.download.url ?? "",
                    mirror_url: quick.download.mirror_url,
                    sha256: quick.download.sha256,
                  }, {
                    index: quick.index?.url ? { url: quick.index.url, mirror_url: quick.index.mirror_url } : null,
                    display_name: quick.download.name.replace(/\.pth$/i, ""),
                  })
                }
                className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
                title="有任务进行中时会加入等待队列，完成后自动开始"
              >
                <CloudDownload className="h-3.5 w-3.5" />一键安装
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void p.openRepo(item)}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
                title="仓库含多个权重，请打开文件面板选择"
              >
                <CloudDownload className="h-3.5 w-3.5" />选文件安装
              </button>
            )}
          </div>
        </div>
      </div>
    </article>
  )
}

// 文件选择 + 安装面板（搜索结果的细化操作）
function RepoFilePanel({ p }: { p: VoiceMarket }) {
  const item = p.repoOpen!
  const pthFiles = (p.repoFiles ?? []).filter((f) => f.type !== "directory" && /\.pth$/i.test(f.path))
  const idxFiles = (p.repoFiles ?? []).filter((f) => f.type !== "directory" && /\.index$/i.test(f.path))
  const zipFiles = (p.repoFiles ?? []).filter((f) => f.type !== "directory" && /\.zip$/i.test(f.path))
  const demo = p.demoAudio(p.repoFiles ?? [])
  const [customId, setCustomId] = useState("")
  const [wantOverwrite, setWantOverwrite] = useState(false)
  const derivedId = p.pickPth ? p.deriveVoiceId(p.pickPth.name, item.repo) : ""
  const finalId = (customId || derivedId).trim()
  const idConflict = p.installed.includes(finalId)

  const handleInstall = () => {
    if (!p.pickPth?.url || !finalId) return
    void p.startInstall(finalId, { url: p.pickPth.url, mirror_url: p.pickPth.mirror_url, sha256: p.pickPth.sha256 }, {
      index: p.pickIdx?.url ? { url: p.pickIdx.url, mirror_url: p.pickIdx.mirror_url } : null,
      display_name: p.pickPth.name.replace(/\.pth$/i, ""),
      overwrite: wantOverwrite,
    })
  }

  return (
    <section className="mt-6 overflow-hidden rounded-2xl border border-border bg-card/85 shadow-lg backdrop-blur-xl">
      <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
        <p className="flex items-center gap-2 text-sm font-medium text-card-foreground">
          <FolderOpen className="h-4 w-4 text-primary" />
          {item.repo}
          <span className="rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">
            {PLATFORM_LABEL[item.platform] ?? item.platform}
          </span>
        </p>
        <button type="button" onClick={() => p.openRepo(item)} className="rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground" aria-label="关闭文件面板">
          <X className="h-4 w-4" />
        </button>
      </div>

      {p.repoLoading ? (
        <div className="flex items-center gap-2 px-5 py-8 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />正在读取仓库文件…
        </div>
      ) : p.repoErr ? (
        <div className="px-5 py-6"><ErrorPanel title="仓库文件加载失败" detail={p.repoErr} hint="可能是网络不可达或仓库格式特殊，可换平台或关键词重试" /></div>
      ) : (
        <div className="grid gap-6 px-5 py-5 lg:grid-cols-[minmax(0,1fr)_260px]">
          <div className="space-y-5">
            {p.repoReadme && (
              <div className="rounded-xl border border-border bg-background/50 p-3">
                <p className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                  <ScrollText className="h-3.5 w-3.5 text-primary" />仓库简介
                </p>
                <p className="text-xs leading-5 text-muted-foreground">{p.repoReadme}</p>
              </div>
            )}
            {demo && (
              <div className="rounded-xl border border-border bg-background/50 p-3">
                <p className="mb-2 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                  <FileAudio className="h-3.5 w-3.5 text-primary" />仓库演示音频
                </p>
                <StudioAudioPlayer src={demo.url ?? demo.path} label="试听演示" />
              </div>
            )}
            {zipFiles.length > 0 && (
              <div className="flex items-start gap-2 rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-[11px] leading-4 text-amber-400">
                <Archive className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>仓库含 {zipFiles.length} 个 zip 包（.pth 打包格式）。一键安装暂不支持 zip，请从仓库主页手动解压后走「音色库 → 导入音色包」。</span>
              </div>
            )}
            <div>
              <p className="mb-2 font-mono text-xs uppercase tracking-widest text-primary">权重文件（.pth，必选）</p>
              {pthFiles.length === 0 ? (
                <p className="text-xs text-muted-foreground">仓库顶层没有 .pth 文件，可能放在子目录（尚未递归展开）或需要 zip 解压。</p>
              ) : (
                <ul className="space-y-1.5">
                  {pthFiles.map((f) => (
                    <FileRow key={f.path} file={f} picked={p.pickPth?.path === f.path} onPick={() => p.setPickPth(f)} />
                  ))}
                </ul>
              )}
            </div>
            <div>
              <p className="mb-2 font-mono text-xs uppercase tracking-widest text-primary">索引文件（.index，可选 · 实时变声更稳）</p>
              {idxFiles.length === 0 ? (
                <p className="text-xs text-muted-foreground">仓库顶层没有 .index 文件，可跳过（仅影响实时变声检索精度）。</p>
              ) : (
                <ul className="space-y-1.5">
                  {idxFiles.map((f) => (
                    <FileRow
                      key={f.path}
                      file={f}
                      picked={p.pickIdx?.path === f.path}
                      onPick={() => p.setPickIdx(p.pickIdx?.path === f.path ? null : f)}
                    />
                  ))}
                </ul>
              )}
            </div>
          </div>

          <aside className="space-y-4">
            <div className="rounded-xl border border-border bg-gradient-to-br from-primary/10 via-card to-card p-4">
              <p className="font-mono text-xs uppercase tracking-widest text-primary">一键安装</p>
              {finalId ? (
                <>
                  <label className="mt-2 block text-[11px] font-medium text-muted-foreground" htmlFor="voice-id-input">
                    音色 ID（可改，仅字母数字_-）
                  </label>
                  <input
                    id="voice-id-input"
                    value={customId}
                    onChange={(e) => {
                      setCustomId(e.target.value.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64))
                      setWantOverwrite(false)
                    }}
                    placeholder={derivedId}
                    className="mt-1 w-full rounded-md border border-border bg-background/60 px-2.5 py-1.5 font-mono text-xs text-card-foreground outline-none transition focus:border-primary focus:ring-1 focus:ring-primary"
                  />
                  <p className="mt-1 text-[11px] text-muted-foreground">安装后可在「音色库 / 实时变声 / 离线工坊」中使用。</p>
                  {idConflict && (
                    <button
                      type="button"
                      onClick={() => setWantOverwrite((v) => !v)}
                      className={`mt-2 flex w-full items-center gap-2 rounded-md border px-2.5 py-1.5 text-left text-[11px] transition ${
                        wantOverwrite
                          ? "border-amber-500/40 bg-amber-500/10 text-amber-400"
                          : "border-border bg-background/40 text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
                      <span>{wantOverwrite ? "已确认：将覆盖现有同名音色" : `ID 已存在（${finalId}），点击确认覆盖重装`}</span>
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={idConflict && !wantOverwrite}
                    onClick={handleInstall}
                    className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    <CloudDownload className="h-4 w-4" />
                    {idConflict ? "覆盖安装" : "开始安装"}
                  </button>
                </>
              ) : (
                <p className="mt-2 text-xs leading-5 text-muted-foreground">先从左侧勾选一个 .pth 权重文件，再开始安装。</p>
              )}
            </div>
            {p.installRunning && (
              <p className="flex items-center gap-1.5 text-xs text-primary">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />有任务进行中：点「开始安装」会加入等待队列，当前任务完成后自动开始。
              </p>
            )}
            <div className="flex items-start gap-2 rounded-lg border border-border bg-background/50 px-3 py-2.5 text-[11px] leading-4 text-muted-foreground">
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
              权重直链仅限白名单域名（HF 官方 / 镜像 / 魔搭），下载完成后校验大小与 SHA256 才落位。
            </div>
          </aside>
        </div>
      )}
    </section>
  )
}

function FileRow({ file, picked, onPick }: { file: MarketFile; picked: boolean; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      className={cn(
        "flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left text-xs transition",
        picked ? "border-primary/60 bg-primary/10" : "border-border bg-background/50 hover:border-primary/40",
      )}
    >
      <span className={cn("flex h-4 w-4 shrink-0 items-center justify-center rounded-full border", picked ? "border-primary bg-primary" : "border-muted-foreground/40")}>
        {picked && <CheckCircle2 className="h-3 w-3 text-primary-foreground" />}
      </span>
      <span className="min-w-0 flex-1 truncate font-mono text-foreground/80" title={file.path}>{file.name}</span>
      <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{fmtBytes(file.size)}</span>
    </button>
  )
}