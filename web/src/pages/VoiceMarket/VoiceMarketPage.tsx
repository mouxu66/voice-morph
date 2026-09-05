import { useState } from "react"
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  CloudDownload,
  FileAudio,
  FolderOpen,
  Loader2,
  Search,
  ScrollText,
  ShieldCheck,
  Store,
  X,
  XCircle,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { StudioAudioPlayer } from "@/components/voice-studio/StudioAudioPlayer"
import { ErrorPanel } from "@/components/ErrorPanel"
import type { MarketFile, MarketItem } from "@/api/client"
import type { VoiceMarket } from "@/pages/VoiceMarket/useVoiceMarket"
import { ACTIVE_PHASE_TEXT, fmtBytes, pctOf } from "@/pages/VoiceMarket/marketFormat"

const PLATFORM_LABEL: Record<string, string> = { hf: "HF", modelscope: "魔搭" }

// ---------- 全局安装进度条 ----------
export function MarketInstallBar(p: Pick<VoiceMarket, "task" | "installRunning" | "installErr" | "setInstallErr" | "cancelInstall">) {
  if (!p.installRunning && !p.installErr) return null
  const t = p.task
  const install = t?.install
  const name = install?.display_name ?? t?.name ?? ""
  const indeterminate = !!t && !t.total && !install?.percent
  const done = (t?.done ?? 0) > 0 && !!t?.total ? fmtBytes(t!.done!) + " / " + fmtBytes(t!.total!) : ""

  return (
    <div className="fixed inset-x-0 bottom-4 z-40 flex justify-center px-4">
      <div className="w-full max-w-2xl rounded-2xl border border-border bg-card/95 p-4 shadow-2xl backdrop-blur-xl">
        {p.installErr ? (
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
        ) : (
          <div className="flex items-center gap-4">
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
      </div>
    </div>
  )
}

// ---------- 推荐 Tab ----------
export function FeaturedTab(p: VoiceMarket) {
  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-5 sm:px-8 lg:px-12">
        <div className="mx-auto flex max-w-7xl flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-widest text-primary">
              <Store className="h-3.5 w-3.5" />VOICE MARKET / 精选清单
            </p>
            <h2 className="mt-2 font-display text-2xl font-bold tracking-tight text-foreground">精选推荐</h2>
            <p className="mt-1.5 text-xs leading-5 text-muted-foreground">精选社区优质 RVC 音色，双源直链一键安装到音色库，装完即可用于实时变声与离线工坊。</p>
          </div>
          {p.manifest && p.manifest.length > 0 && (
            <span className="shrink-0 rounded-full border border-border bg-background/60 px-3 py-1 text-xs text-muted-foreground">
              共 {p.manifest.length} 款 · 双源直链
            </span>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-5 py-6 sm:px-8 lg:px-12 lg:py-8">
        {p.manifest === null ? (
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

function MarketCard({ item, p }: { item: MarketItem; p: VoiceMarket }) {
  const voiceId = item.voice_id ?? ""
  const isInstalled = !!voiceId && p.installed.includes(voiceId)
  const isThis = p.installRunning && p.installingId === voiceId
  const installPct = isThis ? pctOf(p.task) : 0
  const playable = p.isPlayable(item.demo)

  return (
    <article className="flex flex-col rounded-2xl border border-border bg-card/85 p-5 shadow-lg backdrop-blur-xl transition hover:shadow-xl">
      <div className="flex items-center gap-2">
        <span className="rounded-full bg-primary/10 px-2 py-1 font-mono text-[10px] text-primary">{item.category ?? "音色"}</span>
        <span className="rounded-full bg-violet-500/15 px-2 py-1 font-mono text-[10px] text-violet-400">
          {PLATFORM_LABEL[item.platform] ?? item.platform}
        </span>
        {item.size_hint_mb != null && (
          <span className="ml-auto font-mono text-[10px] text-muted-foreground">{fmtBytes(item.size_hint_mb * 1024 * 1024)}</span>
        )}
      </div>
      <h3 className="mt-3 truncate text-base font-semibold text-card-foreground" title={item.name}>{item.name}</h3>
      <p className="mt-1.5 min-h-[2.5rem] text-xs leading-5 text-muted-foreground">{item.desc}</p>
      {item.license && (
        <p className="mt-3 rounded-md border border-amber-500/25 bg-amber-500/10 px-2.5 py-1.5 text-[11px] leading-4 text-amber-400/90">
          <ShieldCheck className="mr-1 inline h-3 w-3 translate-y-[-1px]" />{item.license}
        </p>
      )}
      <div className="mt-4 flex items-center gap-2 border-t border-border pt-4">
        {playable && <StudioAudioPlayer src={item.demo!} label="试听" className="flex-1 min-w-0" />}
        <div className="shrink-0">
          {isInstalled ? (
            <span className="inline-flex items-center gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs font-medium text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5" />已安装
            </span>
          ) : isThis ? (
            <div className="flex items-center gap-2">
              <div className="flex h-9 items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3">
                <div className="h-1 w-16 overflow-hidden rounded-full bg-muted">
                  <div className="h-full rounded-full bg-primary" style={{ width: `${installPct}%` }} />
                </div>
                <span className="font-mono text-[10px] text-primary">{Math.round(installPct)}%</span>
              </div>
            </div>
          ) : (
            <button
              type="button"
              disabled={p.installRunning}
              onClick={() =>
                void p.startInstall(voiceId, item.download!, {
                  index: item.index ?? null,
                  display_name: item.name,
                  manifest_id: item.id,
                })
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3.5 py-2 text-xs font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
            >
              <CloudDownload className="h-3.5 w-3.5" />
              {p.installRunning ? "安装中…" : "一键安装"}
            </button>
          )}
        </div>
      </div>
    </article>
  )
}

// ---------- 搜索 Tab ----------
export function SearchTab(p: VoiceMarket) {
  const [localQuery, setLocalQuery] = useState(p.searchQuery)
  const submit = () => void p.doSearch(localQuery, p.platform)

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <header className="border-b border-border bg-card/30 px-5 py-5 sm:px-8 lg:px-12">
        <div className="mx-auto max-w-7xl">
          <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
            <div className="min-w-0">
              <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-widest text-primary">
                <Search className="h-3.5 w-3.5" />VOICE MARKET / 双源搜索
              </p>
              <h2 className="mt-2 font-display text-2xl font-bold tracking-tight text-foreground">搜索音色仓库</h2>
            </div>
            <p className="max-w-md text-xs leading-5 text-muted-foreground">
              在 HuggingFace 与魔搭检索 RVC 音色；打开仓库选择权重（.pth）与可选索引（.index）后一键安装。
            </p>
          </div>
          <div className="mt-4 flex max-w-3xl flex-col gap-3 sm:flex-row">
            <input
              value={localQuery}
              onChange={(e) => setLocalQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              placeholder="例如 rvc / 音色 / 懒羊羊（魔搭支持 owner/name 精确路径）"
              className="min-w-0 flex-1 rounded-md border border-border bg-background px-3.5 py-2.5 text-sm text-foreground shadow-md outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-primary"
            />
            <div className="flex items-center gap-1 rounded-md border border-border bg-background p-1 shadow-md">
              {(["all", "hf", "modelscope"] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => p.setPlatform(k)}
                  className={cn(
                    "rounded px-2.5 py-1.5 text-xs transition",
                    p.platform === k ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  {k === "all" ? "全部" : PLATFORM_LABEL[k]}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={submit}
              disabled={p.searching || !localQuery.trim()}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-5 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
            >
              {p.searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              {p.searching ? "搜索中…" : "搜索"}
            </button>
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

        {p.results === null ? (
          <div className="rounded-2xl border border-dashed border-border bg-card/60 px-4 py-12 text-center text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            输入关键词开始搜索；「全部」会合并两源结果（各限一半）。
          </div>
        ) : p.results.length === 0 ? (
          <div className="rounded-2xl border border-border bg-card/80 px-4 py-10 text-center text-sm text-muted-foreground shadow-md backdrop-blur-xl">
            没有找到相关仓库，换个关键词试试。
          </div>
        ) : (
          <div className="space-y-3">
            {p.results.map((item) => (
              <SearchResultCard key={item.id} item={item} p={p} />
            ))}
          </div>
        )}

        {p.repoOpen && <RepoFilePanel p={p} />}
      </main>
    </div>
  )
}

function SearchResultCard({ item, p }: { item: MarketItem; p: VoiceMarket }) {
  const prefs = item.prefs
  const quick = p.quickSlot(item)
  const canQuick = !p.installRunning && !!quick
  const quickVoiceId = quick ? p.deriveVoiceId(quick.download.name, item.repo) : ""
  const isInstalled = !!quickVoiceId && p.installed.includes(quickVoiceId)

  return (
    <article className="rounded-2xl border border-border bg-card/85 p-5 shadow-md backdrop-blur-xl transition hover:shadow-lg">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full bg-violet-500/15 px-2 py-0.5 font-mono text-[10px] text-violet-400">
              {PLATFORM_LABEL[item.platform] ?? item.platform}
            </span>
            <h3 className="truncate text-sm font-semibold text-card-foreground">{item.name}</h3>
            {(item.tags_zh ?? item.tags ?? []).slice(0, 3).map((t) => (
              <span key={t} className="rounded-full bg-primary/10 px-2 py-0.5 font-medium text-[10px] text-primary">{t}</span>
            ))}
          </div>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{item.repo}</p>
          {item.desc && (
            <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-muted-foreground">{item.desc}</p>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            {item.downloads != null && item.downloads > 0 && <span>下载 {item.downloads.toLocaleString()}</span>}
            {item.likes != null && item.likes > 0 && <span>点赞 {item.likes.toLocaleString()}</span>}
            {item.updated_at && <span>更新 {item.updated_at}</span>}
            {(item.size_hint_mb != null || (item.files ?? []).length > 0) && (
              <span>{item.files?.length ? `${item.files.length} 个可用文件` : ""}</span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {isInstalled && (
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5" />已安装
            </span>
          )}
          <button
            type="button"
            onClick={() => void p.openRepo(item)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium transition",
              p.repoOpen?.id === item.id
                ? "border-primary/50 bg-primary/10 text-primary"
                : "border-border text-muted-foreground hover:border-primary hover:text-primary",
            )}
          >
            <FolderOpen className="h-3.5 w-3.5" />
            {p.repoOpen?.id === item.id ? "收起" : "查看文件"}
          </button>
          {prefs?.download ? (
            <button
              type="button"
              disabled={p.installRunning}
              onClick={() =>
                void p.startInstall(prefs.voice_id ?? item.voice_id ?? item.id, prefs.download!, {
                  index: prefs.index ?? null,
                  display_name: prefs.name ?? item.name,
                  manifest_id: prefs.id,
                })
              }
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground shadow-md transition hover:scale-105 disabled:pointer-events-none disabled:opacity-50"
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
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground shadow-md transition hover:scale-105 disabled:pointer-events-none disabled:opacity-50"
            >
              <CloudDownload className="h-3.5 w-3.5" />一键安装
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void p.openRepo(item)}
              className="inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-xs text-muted-foreground transition hover:text-primary"
              title="仓库含多个权重，请打开文件面板选择"
            >
              <CloudDownload className="h-3.5 w-3.5" />选文件安装
            </button>
          )}
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
  const idPreview = p.pickPth ? p.deriveVoiceId(p.pickPth.name, item.repo) : ""

  const handleInstall = () => {
    if (!p.pickPth?.url) return
    void p.startInstall(idPreview, { url: p.pickPth.url, mirror_url: p.pickPth.mirror_url, sha256: p.pickPth.sha256 }, {
      index: p.pickIdx?.url ? { url: p.pickIdx.url, mirror_url: p.pickIdx.mirror_url } : null,
      display_name: p.pickPth.name.replace(/\.pth$/i, ""),
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
              {idPreview ? (
                <>
                  <p className="mt-2 text-xs leading-5 text-muted-foreground">
                    音色 ID：<span className="font-mono text-foreground">{idPreview}</span>
                  </p>
                  <p className="mt-1 text-[11px] text-muted-foreground">安装后可在「音色库 / 实时变声 / 离线工坊」中使用。</p>
                  <button
                    type="button"
                    disabled={p.installRunning || p.installed.includes(idPreview)}
                    onClick={handleInstall}
                    className="mt-3 inline-flex w-full items-center justify-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground shadow-md transition hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-50"
                  >
                    <CloudDownload className="h-4 w-4" />
                    {p.installed.includes(idPreview) ? "已安装" : p.installRunning ? "安装中…" : "开始安装"}
                  </button>
                </>
              ) : (
                <p className="mt-2 text-xs leading-5 text-muted-foreground">先从左侧勾选一个 .pth 权重文件，再开始安装。</p>
              )}
            </div>
            {p.installRunning && (
              <p className="flex items-center gap-1.5 text-xs text-primary">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />统一安装队列：当前有任务在跑，完成后自动轮询此窗口。
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