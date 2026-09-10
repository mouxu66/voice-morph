import { useMemo, useState } from "react"
import {
  CheckCircle2,
  CloudDownload,
  Loader2,
  Palette,
  PawPrint,
  Sparkles,
  Trash2,
  X,
  XCircle,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { mediaUrl } from "@/api/client"
import type { PetInstalledItem, PetSkinItem } from "@/api/client"
import type { PetMarket } from "@/pages/PetMarket/usePetMarket"

const CATEGORY_ORDER = ["二次元", "卡通", "像素萌宠", "其他"]
const CATEGORY_TONE: Array<[RegExp, string, string]> = [
  [/二次元/, "from-pink-500/25 to-pink-500/5", "ring-pink-500/30"],
  [/卡通/, "from-amber-500/25 to-amber-500/5", "ring-amber-500/30"],
  [/像素/, "from-emerald-500/25 to-emerald-500/5", "ring-emerald-500/30"],
]
const DEFAULT_TONE = ["from-violet-500/25 to-violet-500/5", "ring-violet-500/30"] as const

function skinBadge(text: string, cls: string) {
  return <span className={cn("rounded-full px-2 py-0.5 font-mono text-[10px]", cls)}>{text}</span>
}

// ---------- 全局安装托盘：进度 + 错误 ----------
export function PetInstallBar(p: PetMarket) {
  const t = p.task
  const active = t && ACTIVE.has(t.status)
  const done = t?.status === "done" && !p.installErr
  if (!active && !done && !p.installErr) return null
  return (
    <div className="fixed inset-x-0 bottom-4 z-40 flex justify-center px-4">
      <div className="w-full max-w-2xl rounded-2xl border border-border bg-card/95 p-4 shadow-2xl backdrop-blur-xl">
        {p.installErr && (
          <div className="flex items-start justify-between gap-3">
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

        {active && t && (
          <div className="flex items-center gap-4">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <CloudDownload className="h-4 w-4 shrink-0 text-primary" />
                <p className="truncate text-sm font-medium text-card-foreground">「{t.skin_id}」安装中</p>
                <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 font-mono text-[10px] text-primary">
                  {t.phase || t.status}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                  <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.max(2, t.percent)}%` }} />
                </div>
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">{t.percent}%</span>
              </div>
              {t.message && <p className="mt-1 truncate text-[11px] text-muted-foreground">{t.message}</p>}
            </div>
          </div>
        )}

        {done && (
          <div className="flex items-center gap-3">
            <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-500" />
            <p className="flex items-center gap-2 text-sm font-medium text-card-foreground">
              安装完成
              <span className="shrink-0 rounded-full bg-emerald-500/10 px-2 py-0.5 font-mono text-[10px] text-emerald-500">已加入皮肤库</span>
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

const ACTIVE = new Set(["downloading", "installing"])

// ---------- 市场主页：推荐清单 + 分类筛选 ----------
export function PetMarketPage(p: PetMarket) {
  const [cat, setCat] = useState<string>("全部")
  const cats = useMemo(() => {
    const set = new Set<string>((p.manifest ?? []).map((m) => m.category ?? "其他"))
    return ["全部", ...CATEGORY_ORDER.filter((c) => set.has(c)), ...[...set].filter((c) => !CATEGORY_ORDER.includes(c))]
  }, [p.manifest])

  const items = useMemo(
    () => (p.manifest ?? []).filter((m) => cat === "全部" || (m.category ?? "其他") === cat),
    [p.manifest, cat],
  )

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
      </header>

      <main className="mx-auto max-w-7xl px-5 py-6 sm:px-8 lg:px-12 lg:py-8">
        {p.manifest === null ? (
          <div className="flex items-center gap-2 rounded-2xl border border-border bg-card/80 px-4 py-6 text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />正在加载皮肤清单…
          </div>
        ) : items.length === 0 ? (
          <div className="rounded-2xl border border-border bg-card/80 px-4 py-8 text-center text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            该分类下暂无皮肤（后端未启动或清单未配置）。
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {items.map((item) => (
              <SkinCard
                key={item.id}
                item={item}
                applied={appliedId === item.id}
                installedItem={installedMap.get(item.id)}
                installing={!!p.task && [item.id].includes(p.task.skin_id) && ACTIVE.has(p.task.status)}
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
    </div>
  )
}

/** 皮肤卡片：预览图（占位按分类着色）+ 标签/许可 + 安装/应用/卸载 */
function SkinCard({ item, applied, installedItem, installing, p }: {
  item: PetSkinItem
  applied: boolean
  installedItem: PetInstalledItem | undefined
  installing: boolean
  p: PetMarket
}) {
  const tone = CATEGORY_TONE.find(([re]) => re.test(item.category ?? "")) ?? null
  const [grad, ring] = tone ?? DEFAULT_TONE
  const preview = installedItem?.preview ? mediaUrl(installedItem.preview) : ""
  const Initial = (item.name || "?").trim().charAt(0).toUpperCase()

  return (
    <article
      className={cn(
        "flex flex-col gap-3 rounded-2xl border bg-card/85 p-4 shadow-md backdrop-blur-xl transition hover:shadow-lg",
        applied ? "border-emerald-500/40" : "border-border",
      )}
    >
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

      {/* 操作 */}
      <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-border pt-3">
        {installing ? (
          <span className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-border bg-background/60 px-3 py-1.5 text-xs font-medium text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />安装中…
          </span>
        ) : installedItem ? (
          <>
            {!applied && (
              <button
                type="button"
                onClick={() => void p.applySkin(item.id)}
                className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
              >
                <Palette className="h-3.5 w-3.5" />
                换肤应用
              </button>
            )}
            {!item.bundle && (
              <button
                type="button"
                onClick={() => {
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
            onClick={() => void p.startInstall(item.id)}
            disabled={!!p.task && ACTIVE.has(p.task.status)}
            className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md bg-blue-600 px-3.5 py-1.5 text-xs font-medium text-white shadow-md transition hover:bg-blue-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 disabled:pointer-events-none disabled:opacity-50"
          >
            <CloudDownload className="h-3.5 w-3.5" />
            一键安装
          </button>
        )}
      </div>
    </article>
  )
}