import { useEffect, useMemo, useRef, useState } from "react"
import {
  CheckCircle2,
  CloudDownload,
  ExternalLink,
  Loader2,
  Palette,
  PawPrint,
  Search,
  Sparkles,
  SquareX,
  Trash2,
  X,
  XCircle,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { mediaUrl } from "@/api/client"
import type { PetInstalledItem, PetSkinDetail, PetSkinItem, PetTaskItem } from "@/api/client"
import type { PetMarket } from "@/pages/PetMarket/usePetMarket"
import { PET_ACTIVE, PET_BUSY } from "@/pages/PetMarket/usePetMarket"

const CATEGORY_ORDER = ["二次元", "卡通", "像素萌宠", "其他"]
const CATEGORY_TONE: Array<[RegExp, string, string]> = [
  [/二次元/, "from-pink-500/25 to-pink-500/5", "ring-pink-500/30"],
  [/卡通/, "from-amber-500/25 to-amber-500/5", "ring-amber-500/30"],
  [/像素/, "from-emerald-500/25 to-emerald-500/5", "ring-emerald-500/30"],
]
const DEFAULT_TONE = ["from-violet-500/25 to-violet-500/5", "ring-violet-500/30"] as const

const STATE_LABEL: Record<string, string> = {
  idle: "待机",
  listen: "聆听",
  think: "思考",
  play: "玩耍",
  build: "干活",
  error: "出错",
}

function skinBadge(text: string, cls: string) {
  return <span className={cn("rounded-full px-2 py-0.5 font-mono text-[10px]", cls)}>{text}</span>
}

function skinName(p: PetMarket, id: string) {
  return p.manifest?.find((m) => m.id === id)?.name ?? id
}

// ---------- 全局安装托盘：多任务队列 + 进度 + 取消 + 完成/失败 ----------
export function PetInstallBar(p: PetMarket) {
  const [dismissed, setDismissed] = useState<Set<string>>(new Set())
  const terminalAt = useRef(new Map<string, number>())   // 首次观察到终结态的时间
  const visible = useMemo(
    () => p.tasks.filter((t) => t.status !== "idle" && !dismissed.has(`${t.skin_id}::${t.status}`)),
    [p.tasks, dismissed],
  )
  // 完成/取消/失败的任务首次出现后 6 秒自动收起（记首次时间，不被 1s 轮询重置）
  useEffect(() => {
    const now = Date.now()
    let maxRemain = 0
    for (const t of visible) {
      if (PET_BUSY.has(t.status)) continue
      const k = `${t.skin_id}::${t.status}`
      if (!terminalAt.current.has(k)) terminalAt.current.set(k, now)
      maxRemain = Math.max(maxRemain, terminalAt.current.get(k)! + 6000 - now)
    }
    if (maxRemain <= 0) return
    const timer = window.setTimeout(() => {
      setDismissed((s) => {
        const next = new Set(s)
        for (const [k, at] of terminalAt.current) {
          if (Date.now() - at >= 6000) next.add(k)
        }
        return next
      })
    }, maxRemain + 80)
    return () => window.clearTimeout(timer)
  }, [visible])

  if (visible.length === 0 && !p.installErr) return null

  const activeN = visible.filter((t) => PET_ACTIVE.has(t.status)).length
  const queuedN = visible.filter((t) => t.status === "queued").length

  return (
    <div className="fixed inset-x-0 bottom-4 z-40 flex justify-center px-4">
      <div className="w-full max-w-2xl rounded-2xl border border-border bg-card/95 p-4 shadow-2xl backdrop-blur-xl">
        {p.installErr && (
          <div className="mb-2 flex items-start justify-between gap-3 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2">
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-2 text-sm font-medium text-destructive">
                <XCircle className="h-4 w-4 shrink-0" />皮肤安装失败
              </p>
              <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{p.installErr}</p>
            </div>
            <button
              type="button"
              onClick={() => p.setInstallErr("")}
              className="rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              aria-label="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        {visible.length > 0 && (
          <>
            <p className="mb-2 flex items-center gap-2 text-xs text-muted-foreground">
              <CloudDownload className="h-3.5 w-3.5 text-primary" />
              {p.tasks.length > 0 ? (
                <span>
                  安装任务队列（
                  <span className="font-medium text-card-foreground">{p.tasks.length}</span>
                  {activeN > 0 && (
                    <>，进行中 <span className="font-medium text-primary">{activeN}</span></>
                  )}
                  {queuedN > 0 && (
                    <>，排队 <span className="font-medium text-amber-400">{queuedN}</span></>
                  )}
                  ）
                </span>
              ) : (
                <span>安装任务队列</span>
              )}
            </p>
            <ul className="flex max-h-64 flex-col gap-2 overflow-y-auto pr-1">
              {visible.map((t) => (
                <TaskRow key={t.skin_id} t={t} name={skinName(p, t.skin_id)} p={p} />
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  )
}

function TaskRow({ t, name, p }: { t: PetTaskItem; name: string; p: PetMarket }) {
  const active = PET_ACTIVE.has(t.status)
  const queued = t.status === "queued"
  const done = t.status === "done"
  const failed = t.status === "failed"
  const cancelled = t.status === "cancelled"
  return (
    <li
      className={cn(
        "flex items-center gap-3 rounded-xl border px-3 py-2.5",
        active && "border-primary/30 bg-primary/5",
        queued && "border-border bg-muted/40",
        done && "border-emerald-500/30 bg-emerald-500/5",
        failed && "border-destructive/40 bg-destructive/10",
        cancelled && "border-border bg-muted/40 opacity-70",
      )}
    >
      {active ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin text-primary" />
      ) : done ? (
        <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
      ) : failed ? (
        <XCircle className="h-4 w-4 shrink-0 text-destructive" />
      ) : cancelled ? (
        <SquareX className="h-4 w-4 shrink-0 text-muted-foreground" />
      ) : (
        <CloudDownload className="h-4 w-4 shrink-0 text-amber-400" />
      )}

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate text-sm font-medium text-card-foreground">{name}</p>
          <span
            className={cn(
              "shrink-0 rounded-full px-2 py-0.5 font-mono text-[10px]",
              active && "bg-primary/10 text-primary",
              queued && "bg-amber-500/10 text-amber-400",
              done && "bg-emerald-500/10 text-emerald-500",
              failed && "bg-destructive/10 text-destructive",
              cancelled && "bg-muted text-muted-foreground",
            )}
          >
            {queued ? `${t.phase}` : t.phase || t.status}
          </span>
        </div>
        {active && (
          <div className="mt-1.5 flex items-center gap-2">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.max(2, t.percent)}%` }} />
            </div>
            <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{t.percent}%</span>
          </div>
        )}
        {active && t.message && <p className="mt-1 truncate text-[11px] text-muted-foreground">{t.message}</p>}
        {failed && t.error && <p className="mt-1 break-words text-[11px] leading-4 text-destructive/90">{t.error}</p>}
        {done && <p className="mt-0.5 text-[11px] text-emerald-600">已加入皮肤库，可在详情中换肤应用</p>}
        {cancelled && <p className="mt-0.5 text-[11px] text-muted-foreground">已停止安装</p>}
      </div>

      {(active || queued) && (
        <button
          type="button"
          onClick={() => void p.cancelTask(t.skin_id)}
          disabled={!active && !queued}
          className="shrink-0 inline-flex items-center gap-1 rounded-md border border-border bg-background/60 px-2 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-destructive/40 hover:text-destructive disabled:pointer-events-none disabled:opacity-50"
        >
          <SquareX className="h-3.5 w-3.5" />
          {queued ? "移除" : "取消"}
        </button>
      )}
    </li>
  )
}

// ---------- 市场主页：搜索 + 分类筛选 + 皮肤卡片 ----------
export function PetMarketPage(p: PetMarket) {
  const [cat, setCat] = useState<string>("全部")
  const [query, setQuery] = useState("")

  const cats = useMemo(() => {
    const set = new Set<string>((p.manifest ?? []).map((m) => m.category ?? "其他"))
    return ["全部", ...CATEGORY_ORDER.filter((c) => set.has(c)), ...[...set].filter((c) => !CATEGORY_ORDER.includes(c))]
  }, [p.manifest])

  const items = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (p.manifest ?? []).filter((m) => {
      if (cat !== "全部" && (m.category ?? "其他") !== cat) return false
      if (!q) return true
      const hay = [m.id, m.name, m.description, m.category, m.attribution, m.license]
        .filter(Boolean).join(" ").toLowerCase()
      return hay.includes(q)
    })
  }, [p.manifest, cat, query])

  const installedMap = useMemo(
    () => new Map((p.installed ?? []).map((i) => [i.id, i])),
    [p.installed],
  )
  const appliedId = p.applied?.id ?? ""

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="sticky top-0 z-30 border-b border-border bg-card/80 px-5 py-4 backdrop-blur-xl sm:px-8 lg:px-12">
        <div className="mx-auto max-w-7xl">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h1 className="flex items-center gap-2 text-lg font-semibold tracking-tight text-foreground">
                <PawPrint className="h-5 w-5 text-primary" />
                人偶市场
              </h1>
              <p className="mt-0.5 text-xs text-muted-foreground">
                给桌面人偶换个外观：全部素材来自开源社区、许可可分发，一键安装即时换肤。
              </p>
            </div>
            <div className="flex flex-col gap-2 sm:items-end">
              <div className="relative w-full sm:w-64">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="搜索皮肤 / 分类 / 作者…"
                  className="w-full rounded-full border border-border bg-background/70 py-2 pl-9 pr-9 text-sm text-foreground shadow-sm outline-none transition placeholder:text-muted-foreground/70 focus:border-primary/50 focus:ring-2 focus:ring-primary/20"
                  aria-label="搜索皮肤"
                />
                {query && (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
                    aria-label="清空搜索"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {cats.map((c) => (
                  <button
                    key={c}
                    type="button"
                    onClick={() => setCat(c)}
                    className={cn(
                      "rounded-full border px-3.5 py-1.5 text-xs font-medium transition",
                      cat === c
                        ? "border-primary/50 bg-primary/10 text-primary"
                        : "border-border text-muted-foreground hover:border-primary/40 hover:text-foreground",
                    )}
                  >
                    {c}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-5 py-6 sm:px-8 lg:px-12 lg:py-8">
        {p.manifest === null ? (
          <div className="flex items-center gap-2 rounded-2xl border border-border bg-card/80 px-4 py-6 text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />正在加载皮肤清单…
          </div>
        ) : items.length === 0 ? (
          <div className="rounded-2xl border border-border bg-card/80 px-4 py-8 text-center text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            {query || cat !== "全部" ? "没有匹配的皮肤，换个关键词或分类试试。" : "该分类下暂无皮肤（后端未启动或清单未配置）。"}
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {items.map((item) => (
              <SkinCard
                key={item.id}
                item={item}
                applied={appliedId === item.id}
                installedItem={installedMap.get(item.id)}
                p={p}
              />
            ))}
          </div>
        )}

        <div className="mt-6 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs leading-5 text-amber-400">
          <Sparkles className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            素材均来自开源社区并按各自许可分发（MIT / Apache-2.0 / CC0 等），收到皮肤包时会附上来源与许可文件。请遵守对应许可并勿商用受限素材。
          </p>
        </div>
      </main>

      <PetDetailDrawer p={p} />
    </div>
  )
}

/** 皮肤卡片：预览图 + 标签/许可 + 安装/应用/卸载；点击进入详情 */
function SkinCard({ item, applied, installedItem, p }: {
  item: PetSkinItem
  applied: boolean
  installedItem: PetInstalledItem | undefined
  p: PetMarket
}) {
  const tone = CATEGORY_TONE.find(([re]) => re.test(item.category ?? "")) ?? null
  const [grad, ring] = tone ?? DEFAULT_TONE
  const preview = installedItem?.preview ? mediaUrl(installedItem.preview) : ""
  const Initial = (item.name || "?").trim().charAt(0).toUpperCase()
  const busy = p.tasks.some((t) => t.skin_id === item.id && PET_BUSY.has(t.status))

  return (
    <article
      onClick={() => void p.openDetail(item.id)}
      className={cn(
        "group flex cursor-pointer flex-col gap-3 rounded-2xl border bg-card/85 p-4 shadow-md backdrop-blur-xl transition hover:shadow-lg",
        applied ? "border-emerald-500/40" : "border-border",
      )}
    >
      <div className="flex gap-3">
        {/* 预览区 */}
        <div className={cn("relative h-28 w-28 shrink-0 overflow-hidden rounded-xl bg-gradient-to-br ring-1", grad, ring)}>
          {preview ? (
            <img src={preview} alt="" className="h-full w-full object-contain" />
          ) : (
            <span className="flex h-full w-full items-center justify-center font-display text-3xl font-semibold text-foreground/80">
              {Initial}
            </span>
          )}
          <div className="absolute bottom-1 right-1 flex gap-1">
            {applied && skinBadge("使用中", "bg-emerald-500/90 text-emerald-50 shadow-sm")}
          </div>
        </div>

        {/* 信息 */}
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-1.5">
            {skinBadge(item.category ?? "其他", "bg-primary/10 text-primary")}
            {installedItem && skinBadge("已安装", "bg-emerald-500/15 text-emerald-400")}
            {item.bundle && skinBadge("内置", "bg-muted text-muted-foreground")}
          </div>
          <h3 className="truncate text-base font-semibold text-card-foreground" title={item.name}>{item.name}</h3>
          <p className="line-clamp-2 min-h-[2.5rem] text-xs leading-5 text-muted-foreground">{item.description}</p>
          {item.attribution && (
            <p className="truncate text-[11px] text-muted-foreground/80" title={`来源：${item.attribution}`}>
              来源：{item.attribution}
            </p>
          )}
          <p className="rounded-md border border-amber-500/25 bg-amber-500/10 px-2 py-1 text-[11px] leading-4 text-amber-400/90">
            许可：{item.license ?? "未标注"}
          </p>
        </div>
      </div>

      {/* 操作 */}
      <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
        {busy ? (
          <span className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs font-medium text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />安装中…
          </span>
        ) : installedItem ? (
          <>
            {!applied && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); void p.applySkin(item.id) }}
                className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
              >
                <Palette className="h-3.5 w-3.5" />
                换肤应用
              </button>
            )}
            {!item.bundle && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  if (!window.confirm(`确定卸载「${item.name}」皮肤吗？`)) return
                  void p.uninstallSkin(item.id)
                }}
                className="inline-flex items-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-1.5 text-xs font-medium text-destructive transition hover:bg-destructive/20"
                aria-label={`卸载 ${item.name}`}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            )}
          </>
        ) : (
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); void p.startInstall(item.id) }}
            className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
          >
            <CloudDownload className="h-3.5 w-3.5" />
            一键安装
          </button>
        )}
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); void p.openDetail(item.id) }}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs font-medium text-muted-foreground transition hover:border-primary/40 hover:text-foreground"
        >
          详情
        </button>
      </div>
    </article>
  )
}

// ---------- 皮肤详情抽屉：动画预览 / 许可全文 / 来源链接 / 帧尺寸 / 状态表 ----------
function PetDetailDrawer({ p }: { p: PetMarket }) {
  const open = p.detailId !== ""
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50">
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={p.closeDetail}
        aria-hidden="true"
      />
      <aside className="absolute inset-y-0 right-0 flex w-[min(92vw,420px)] flex-col border-l border-border bg-card/95 shadow-2xl backdrop-blur-xl">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <h2 className="flex items-center gap-2 text-base font-semibold text-foreground">
            <PawPrint className="h-4 w-4 text-primary" />
            皮肤详情
          </h2>
          <button
            type="button"
            onClick={p.closeDetail}
            className="rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground"
            aria-label="关闭详情"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {p.detailLoading ? (
            <div className="flex h-40 items-center justify-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin text-primary" />加载详情…
            </div>
          ) : p.detail ? (
            <DetailBody detail={p.detail} p={p} />
          ) : (
            <p className="text-sm text-muted-foreground">无法加载皮肤详情，请稍后重试。</p>
          )}
        </div>
      </aside>
    </div>
  )
}

function DetailBody({ detail, p }: { detail: PetSkinDetail; p: PetMarket }) {
  const item = detail.item
  const busy = p.tasks.some((t) => t.skin_id === item.id && PET_BUSY.has(t.status))
  return (
    <div className="flex flex-col gap-4">
      {/* 动画预览 */}
      <div className="flex items-center justify-center rounded-2xl border border-border bg-gradient-to-br from-primary/10 via-transparent to-accent/10 py-5">
        <SkinAnim detail={detail} size={132} />
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {skinBadge(item.category ?? "其他", "bg-primary/10 text-primary")}
        {detail.installed && skinBadge("已安装", "bg-emerald-500/15 text-emerald-400")}
        {detail.applied && skinBadge("使用中", "bg-emerald-500/90 text-emerald-50")}
        {item.bundle && skinBadge("内置", "bg-muted text-muted-foreground")}
        {skinBadge(item.license ?? "未标注", "bg-amber-500/10 text-amber-400")}
      </div>

      <div>
        <h3 className="text-lg font-semibold text-card-foreground">{item.name}</h3>
        {item.description && (
          <p className="mt-1 text-sm leading-6 text-muted-foreground">{item.description}</p>
        )}
        {item.attribution && (
          <p className="mt-2 text-xs text-muted-foreground/80">来源：{item.attribution}</p>
        )}
      </div>

      {/* 技术参数 */}
      {(detail.frameW > 0 || Object.keys(detail.states).length > 0) && (
        <div className="rounded-xl border border-border bg-background/50 p-3">
          <p className="text-xs font-medium text-muted-foreground">动画规格</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {detail.frameW > 0 && (
              <span className="rounded-md bg-muted px-2 py-1 font-mono text-[11px] text-foreground/80">
                帧 {detail.frameW}×{detail.frameH}
              </span>
            )}
            {Object.entries(detail.states).map(([state, s]) => (
              <span
                key={state}
                className="rounded-md bg-muted/70 px-2 py-1 font-mono text-[11px] text-foreground/80"
                title={`${s.frames} 帧 / ${s.dur}s`}
              >
                {STATE_LABEL[state] ?? state}·{s.frames}帧
              </span>
            ))}
          </div>
        </div>
      )}

      {/* 来源链接 */}
      {detail.source_urls.length > 0 && (
        <div className="rounded-xl border border-border bg-background/50 p-3">
          <p className="text-xs font-medium text-muted-foreground">素材来源（许可原文见附属 LICENSE）</p>
          <ul className="mt-2 flex flex-col gap-1.5">
            {detail.source_urls.map((u) => (
              <li key={u}>
                <a
                  href={u}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 break-all text-xs text-primary underline-offset-2 hover:underline"
                >
                  <ExternalLink className="h-3 w-3 shrink-0" />
                  {u}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 许可全文 */}
      <div className="rounded-xl border border-border bg-background/50 p-3">
        <p className="text-xs font-medium text-muted-foreground">LICENSE 许可全文</p>
        {detail.license_text ? (
          <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-foreground/80">
            {detail.license_text}
          </pre>
        ) : (
          <p className="mt-2 text-xs text-muted-foreground/70">
            安装后即可查看该素材的许可全文与署名信息。
          </p>
        )}
      </div>

      {/* 操作 */}
      <div className="flex gap-2 border-t border-border pt-4">
        {busy ? (
          <span className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-2 text-xs font-medium text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />安装中…
          </span>
        ) : detail.installed ? (
          <>
            {!detail.applied && (
              <button
                type="button"
                onClick={() => void p.applySkin(item.id)}
                className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-2 text-sm font-medium text-white shadow-md transition hover:bg-blue-500"
              >
                <Palette className="h-4 w-4" />
                换肤应用
              </button>
            )}
            {!item.bundle && (
              <button
                type="button"
                onClick={() => {
                  if (!window.confirm(`确定卸载「${item.name}」皮肤吗？`)) return
                  void p.uninstallSkin(item.id).then(() => p.closeDetail())
                }}
                className="inline-flex items-center justify-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-3.5 py-2 text-sm font-medium text-destructive transition hover:bg-destructive/20"
              >
                <Trash2 className="h-4 w-4" />
                卸载
              </button>
            )}
          </>
        ) : (
          <button
            type="button"
            onClick={() => void p.startInstall(item.id)}
            className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-2 text-sm font-medium text-white shadow-md transition hover:bg-blue-500"
          >
            <CloudDownload className="h-4 w-4" />
            一键安装
          </button>
        )}
      </div>
    </div>
  )
}

/** 用 spritesheet 作背景 + steps 步进做动画预览（任意帧尺寸自适应） */
function SkinAnim({ detail, size = 96 }: { detail: PetSkinDetail; size?: number }) {
  const states = detail.states ?? {}
  const st = states["idle"] ?? Object.values(states)[0]
  const fw = detail.frameW || 64
  const fh = detail.frameH || 64
  const frames = st?.frames ?? 1
  const dur = st?.dur ?? 3
  const key = `petplay${detail.item.id.replace(/[^a-zA-Z0-9]/g, "")}`
  const sheetUrl =
    detail.installed && st?.sheet
      ? mediaUrl(`/api/pet-market/sheet/${encodeURIComponent(detail.item.id)}/${encodeURIComponent(st.sheet)}`)
      : ""
  if (!sheetUrl) {
    return (
      <div className="flex h-24 w-24 items-center justify-center rounded-xl bg-muted/50 font-display text-4xl font-semibold text-muted-foreground/60">
        {(detail.item.name || "?").charAt(0).toUpperCase()}
      </div>
    )
  }
  const scale = size / fw
  return (
    <div style={{ width: size, height: fh * scale }}>
      <style>{`@keyframes ${key} { from { background-position: 0 0; } to { background-position: ${-(fw * frames)}px 0; } }`}</style>
      <div
        style={{
          width: fw,
          height: fh,
          transform: `scale(${scale})`,
          transformOrigin: "top left",
          backgroundImage: `url(${sheetUrl})`,
          backgroundRepeat: "no-repeat",
          backgroundSize: `${fw * frames}px ${fh}px`,
          animation: frames > 1 ? `${key} ${dur}s steps(${frames}) infinite` : "none",
        }}
      />
    </div>
  )
}