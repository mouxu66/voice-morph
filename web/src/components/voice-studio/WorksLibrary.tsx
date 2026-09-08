import { useCallback, useEffect, useMemo, useState } from "react"
import {
  CheckSquare, Download, Loader2, Package, RefreshCw, Square, Star, Tag, Trash2, X,
} from "lucide-react"
import {
  bulkDeleteHistory, exportHistoryZip, historyTags, listHistory, mediaUrl,
  patchHistoryMeta,
} from "@/api/client"
import type { HistoryItem, TagCount } from "@/api/client"
import { StudioAudioPlayer } from "./StudioAudioPlayer"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"
import { voiceOptionLabel } from "@/lib/voiceLabel"

const PAGE = 30

const KIND_LABEL: Record<string, string> = {
  tts: "语音合成",
  offlinevc: "离线变声",
  audiobook: "有声书",
  fx: "特效",
  trial: "试听",
  mine: "挖掘",
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function WorksLibrary({ voices }: { voices: { id: string; display_name?: string }[] }) {
  const [items, setItems] = useState<HistoryItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  const [msg, setMsg] = useState("")
  // 筛选：音色 / 只看收藏 / 标签
  const [voiceFilter, setVoiceFilter] = useState("")
  const [onlyStarred, setOnlyStarred] = useState(false)
  const [tagFilter, setTagFilter] = useState("")
  const [tags, setTags] = useState<TagCount[]>([])
  const [checked, setChecked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)

  const load = useCallback(async (offset = 0) => {
    setLoading(true)
    setError("")
    try {
      const [r, t] = await Promise.all([
        listHistory({
          voice_id: voiceFilter || undefined,
          starred: onlyStarred || undefined,
          tag: tagFilter || undefined,
          limit: PAGE, offset,
        }),
        historyTags().catch(() => [] as TagCount[]),
      ])
      setItems(offset === 0 ? r.items : (cur) => [...cur, ...r.items])
      setTotal(r.total)
      setTags(t)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [voiceFilter, onlyStarred, tagFilter])

  useEffect(() => { void load(0) }, [load])

  const nameOf = (id: string) => voices.find((v) => v.id === id)?.display_name ?? id

  const toggleStar = async (item: HistoryItem) => {
    try {
      const updated = await patchHistoryMeta(item.id, { starred: !item.starred })
      setItems((cur) => cur.map((x) => (x.id === updated.id ? updated : x)))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const editTags = async (item: HistoryItem) => {
    const raw = window.prompt("标签（逗号或空格分隔，最多 8 个、每个 16 字内）：", item.tags.join(" "))
    if (raw === null) return
    try {
      const updated = await patchHistoryMeta(item.id, { tags: raw.split(/[\s,，;；]+/).filter(Boolean) })
      setItems((cur) => cur.map((x) => (x.id === updated.id ? updated : x)))
      historyTags().then(setTags).catch(() => {})
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const selected = useMemo(() => [...checked], [checked])
  const anyChecked = selected.length > 0

  const runBulk = async (action: "export" | "delete") => {
    if (!anyChecked) return
    if (action === "delete" && !window.confirm(
      `删除选中的 ${selected.length} 条作品？\n对应的音频文件会一并从磁盘删除，不可恢复。`,
    )) return
    setBusy(true)
    setError("")
    setMsg("")
    try {
      if (action === "export") {
        const { missing } = await exportHistoryZip(selected)
        setMsg(missing > 0
          ? `已打包下载（${missing} 个文件在磁盘上已不存在，已跳过）`
          : `已打包下载 ${selected.length} 个作品`)
      } else {
        const r = await bulkDeleteHistory(selected)
        setMsg(`已删除 ${r.deleted} 条` + (r.failed.length ? `，${r.failed.length} 条失败` : ""))
      }
      setChecked(new Set())
      await load(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const allOnPageChecked = items.length > 0 && items.every((x) => checked.has(x.id))

  return (
    <div className="rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-widest text-primary">WORKS LIBRARY</p>
          <h3 className="mt-2 text-lg font-semibold text-card-foreground">作品库</h3>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            语音合成 / 离线变声 / 有声书等全部产出都在这里。可收藏、打标签、按音色筛选，
            勾选后可批量打包成 zip 或删除。
          </p>
        </div>
        <button
          type="button"
          onClick={() => { setChecked(new Set()); void load(0) }}
          className="rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground shadow-md transition hover:border-primary hover:text-primary"
        >
          <RefreshCw className="mr-1.5 inline h-3.5 w-3.5" />刷新
        </button>
      </div>

      {/* 筛选行 */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <select
          value={voiceFilter}
          onChange={(e) => setVoiceFilter(e.target.value)}
          className="rounded-md border border-border bg-background px-3 py-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary"
          aria-label="按音色筛选"
        >
          <option value="">全部音色</option>
          {voices.map((v) => (
            <option key={v.id} value={v.id}>{voiceOptionLabel(v)}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setOnlyStarred((v) => !v)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium transition",
            onlyStarred
              ? "border-amber-500/50 bg-amber-500/10 text-amber-500"
              : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary",
          )}
        >
          <Star className={cn("h-3.5 w-3.5", onlyStarred && "fill-amber-500")} />只看收藏
        </button>
        {tags.map((t) => (
          <button
            key={t.tag}
            type="button"
            onClick={() => setTagFilter((cur) => (cur === t.tag ? "" : t.tag))}
            className={cn(
              "inline-flex items-center gap-1 rounded-full border px-2.5 py-1.5 text-xs transition",
              tagFilter === t.tag
                ? "border-primary bg-primary/15 text-primary"
                : "border-border bg-background text-muted-foreground hover:border-primary hover:text-primary",
            )}
          >
            <Tag className="h-3 w-3" />{t.tag}
            <span className="font-mono text-[10px] opacity-70">{t.count}</span>
          </button>
        ))}
      </div>

      {/* 批量操作条 */}
      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-border pt-4">
        <button
          type="button"
          onClick={() => setChecked(allOnPageChecked ? new Set() : new Set(items.map((x) => x.id)))}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-primary"
        >
          {allOnPageChecked ? <CheckSquare className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5" />}
          {allOnPageChecked ? "取消全选" : "全选本页"}
        </button>
        <span className="text-xs text-muted-foreground">
          共 {total} 条{anyChecked && ` · 已选 ${selected.length}`}
        </span>
        {anyChecked && (
          <>
            <button
              type="button"
              disabled={busy}
              onClick={() => void runBulk("export")}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground shadow-md transition hover:scale-105 disabled:opacity-50"
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Package className="h-3.5 w-3.5" />}
              打包下载 zip
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void runBulk("delete")}
              className="inline-flex items-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs font-medium text-destructive transition hover:bg-destructive/20 disabled:opacity-50"
            >
              <Trash2 className="h-3.5 w-3.5" />删除
            </button>
          </>
        )}
      </div>

      {error && <ErrorPanel title="作品库操作失败" detail={error} className="mt-3" />}
      {msg && (
        <p className="mt-3 flex items-center gap-2 rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-xs text-primary">
          <Download className="h-3.5 w-3.5" />{msg}
        </p>
      )}

      {/* 列表 */}
      {!loading && !items.length ? (
        <p className="mt-5 rounded-lg border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
          还没有作品——去「语音合成」「离线工坊」产出第一条吧。
        </p>
      ) : (
        <ul className="mt-4 space-y-3">
          {items.map((item) => {
            const isOn = checked.has(item.id)
            return (
              <li
                key={item.id}
                className={cn(
                  "rounded-lg border bg-background/60 p-4 transition",
                  isOn ? "border-primary/70" : "border-border",
                )}
              >
                <div className="flex items-start gap-3">
                  <button
                    type="button"
                    onClick={() => setChecked((cur) => {
                      const next = new Set(cur)
                      if (next.has(item.id)) next.delete(item.id)
                      else next.add(item.id)
                      return next
                    })}
                    className="mt-0.5 shrink-0 text-muted-foreground transition hover:text-primary"
                    aria-label={isOn ? "取消勾选" : "勾选"}
                  >
                    {isOn ? <CheckSquare className="h-4 w-4 text-primary" /> : <Square className="h-4 w-4" />}
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                        {KIND_LABEL[item.kind] ?? item.kind}
                      </span>
                      <span className="truncate text-xs font-medium text-card-foreground">
                        {item.voice_id ? nameOf(item.voice_id) : "未指定音色"}
                      </span>
                      <span className="font-mono text-[10px] text-muted-foreground">
                        {fmtTime(item.ts)} · {item.duration_s}s
                      </span>
                      <button
                        type="button"
                        onClick={() => void toggleStar(item)}
                        className="ml-auto shrink-0 transition hover:scale-110"
                        aria-label={item.starred ? "取消收藏" : "收藏"}
                      >
                        <Star className={cn("h-4 w-4", item.starred ? "fill-amber-400 text-amber-400" : "text-muted-foreground")} />
                      </button>
                      <button
                        type="button"
                        onClick={() => void editTags(item)}
                        className="shrink-0 text-muted-foreground transition hover:text-primary"
                        aria-label="编辑标签"
                      >
                        <Tag className="h-3.5 w-3.5" />
                      </button>
                    </div>
                    {item.input_text && (
                      <p className="mt-1 truncate text-[11px] text-muted-foreground">{item.input_text}</p>
                    )}
                    {item.tags.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {item.tags.map((t) => (
                          <span key={t} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{t}</span>
                        ))}
                      </div>
                    )}
                    <StudioAudioPlayer src={mediaUrl(item.url)} label="播放" className="mt-2" />
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {items.length < total && (
        <button
          type="button"
          disabled={loading}
          onClick={() => void load(items.length)}
          className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-md border border-border bg-background px-4 py-2.5 text-xs text-muted-foreground transition hover:border-primary hover:text-primary disabled:opacity-50"
        >
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          加载更多（{items.length}/{total}）
        </button>
      )}
      {loading && !items.length && (
        <div className="mt-4 space-y-3">{[1, 2, 3].map((i) => (
          <div key={i} className="h-20 animate-pulse rounded-lg border border-border bg-card" />
        ))}</div>
      )}
      <p className="mt-4 flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <X className="h-3 w-3" />删除会同时删掉磁盘上的音频文件；打包导出只包含仍在磁盘上的产物。
      </p>
    </div>
  )
}
