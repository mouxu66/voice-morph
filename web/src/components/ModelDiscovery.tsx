import { useState } from "react"
import {
  AlertTriangle,
  BadgeCheck,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  FolderSearch,
  Info,
  Loader2,
  RefreshCw,
  Sparkles,
} from "lucide-react"
import { cn } from "@/lib/utils"
import type { ResourceGuide, ScanCandidate, ScanResult, SetupKind } from "@/lib/electron"

/** 复制到剪贴板：桌面壳里 `navigator.clipboard` 不一定可用（取决于加载来源），
 *  因此带一个 execCommand 兜底 —— 复制命令是这段 UI 的主要用途，不能悄悄失效。 */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    /* 落到兜底 */
  }
  try {
    const ta = document.createElement("textarea")
    ta.value = text
    ta.style.position = "fixed"
    ta.style.opacity = "0"
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand("copy")
    ta.remove()
    return ok
  } catch {
    return false
  }
}

const KIND_SHORT: Record<SetupKind, string> = {
  tts_models: "TTS 模型",
  tts_venv: "TTS 解释器",
  rvc_root: "RVC 整合包",
}

/** 扫描到的一个候选位置：一行路径 + 判定理由 + 「使用」 */
function CandidateRow({
  cand,
  busy,
  onUse,
}: {
  cand: ScanCandidate
  busy: boolean
  onUse: (cand: ScanCandidate) => void
}) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-3 rounded-md border px-2.5 py-2",
        cand.recommended ? "border-primary/30 bg-primary/5" : "border-border bg-background/60",
      )}
    >
      <div className="min-w-0">
        <p className="flex items-center gap-1.5">
          <span className="break-all font-mono text-[11px] leading-4 text-foreground">{cand.path}</span>
          {cand.recommended && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-medium text-primary">
              <BadgeCheck className="h-3 w-3" />
              推荐
            </span>
          )}
        </p>
        <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
          {cand.reasons.join(" · ")}
        </p>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={() => onUse(cand)}
        className={cn(
          "shrink-0 rounded-md px-2.5 py-1 text-[11px] font-medium transition disabled:opacity-50",
          cand.recommended
            ? "bg-primary text-primary-foreground hover:bg-primary/90"
            : "border border-border bg-background text-muted-foreground hover:text-foreground",
        )}
      >
        使用
      </button>
    </div>
  )
}

/**
 * 一项资源的候选列表（无候选时给出"没扫到 + 去哪下"的引导）。
 * 只在对应项未就绪时展示 —— 已经配好的项不需要我们再劝。
 */
export function DiscoveryList({
  kind,
  candidates,
  scanning,
  scanned,
  busy,
  onUse,
  onOpenGuide,
  guideOpen,
  children,
}: {
  kind: SetupKind
  candidates: ScanCandidate[]
  scanning: boolean
  scanned: boolean
  busy: boolean
  onUse: (cand: ScanCandidate) => void
  onOpenGuide: () => void
  guideOpen: boolean
  children?: React.ReactNode
}) {
  return (
    <div className="mt-2 space-y-1.5 border-t border-border/60 pt-2">
      {scanning && (
        <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" /> 正在扫描本机…
        </p>
      )}

      {!scanning && candidates.length > 0 && (
        <>
          <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <FolderSearch className="h-3 w-3" />
            在本机扫到 {candidates.length} 处可用位置：
          </p>
          <div className="space-y-1.5">
            {candidates.map((c) => (
              <CandidateRow key={c.path} cand={c} busy={busy} onUse={onUse} />
            ))}
          </div>
        </>
      )}

      {!scanning && scanned && candidates.length === 0 && (
        <p className="flex items-start gap-1.5 text-[11px] leading-4 text-muted-foreground">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-yellow-600" />
          <span>
            本机没扫到可用的{ KIND_SHORT[kind] }。要么还没下载，要么放在了我们不检查的位置 ——
            下面有官方下载链接与放置说明。
          </span>
        </p>
      )}

      <button
        type="button"
        onClick={onOpenGuide}
        className="flex items-center gap-1 text-[11px] font-medium text-primary transition hover:underline"
      >
        {guideOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <Download className="h-3 w-3" />
        去哪下载 / 怎么放
      </button>
      {children}
    </div>
  )
}

/** 指引正文：体积 / 目标布局 / 分步命令 / 官方链接 / 易踩的坑 */
export function GuideBlock({
  guide,
  onOpenLink,
  verifiedAt,
}: {
  guide: ResourceGuide
  onOpenLink: (index: number) => void
  verifiedAt?: string
}) {
  const [copied, setCopied] = useState<string | null>(null)

  const handleCopy = async (text: string) => {
    const ok = await copyText(text)
    setCopied(ok ? text : null)
    if (ok) window.setTimeout(() => setCopied((c) => (c === text ? null : c)), 2000)
  }

  return (
    <div className="mt-2 space-y-3 rounded-lg border border-border bg-background/60 p-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        <span className="font-medium text-card-foreground">{guide.label}</span>
        <span>体积 {guide.sizeText}</span>
        {verifiedAt && <span className="opacity-70">链接核对于 {verifiedAt}</span>}
      </div>
      <p className="text-[11px] leading-4 text-muted-foreground">{guide.why}</p>

      <div>
        <p className="text-[11px] font-medium text-card-foreground">下载后要放成这样</p>
        <pre className="mt-1 overflow-x-auto rounded-md bg-muted px-2.5 py-2 font-mono text-[11px] leading-4 text-foreground">
          {guide.layout.join("\n")}
        </pre>
      </div>

      <div className="space-y-2">
        {guide.steps.map((s) => (
          <div key={s.title}>
            <p className="text-[11px] font-medium text-card-foreground">{s.title}</p>
            {s.detail && (
              <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{s.detail}</p>
            )}
            {s.command && (
              <div className="relative mt-1">
                <pre className="overflow-x-auto rounded-md bg-muted px-2.5 py-2 pr-16 font-mono text-[11px] leading-4 text-foreground">
                  {s.command}
                </pre>
                <button
                  type="button"
                  onClick={() => void handleCopy(s.command as string)}
                  className="absolute right-1.5 top-1.5 flex items-center gap-1 rounded border border-border bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground transition hover:text-foreground"
                >
                  {copied === s.command ? (
                    <>
                      <Check className="h-3 w-3 text-primary" /> 已复制
                    </>
                  ) : (
                    <>
                      <Copy className="h-3 w-3" /> 复制
                    </>
                  )}
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      <div>
        <p className="text-[11px] font-medium text-card-foreground">官方链接</p>
        <div className="mt-1 flex flex-wrap gap-1.5">
          {guide.links.map((l, i) => (
            <button
              key={l.url}
              type="button"
              onClick={() => onOpenLink(i)}
              title={l.url}
              className="flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-[11px] text-muted-foreground transition hover:text-foreground"
            >
              <ExternalLink className="h-3 w-3 shrink-0" />
              {l.label}
              {l.note && <span className="text-muted-foreground/70">（{l.note}）</span>}
            </button>
          ))}
        </div>
      </div>

      {guide.notes.length > 0 && (
        <div className="space-y-1.5 rounded-md border border-yellow-500/40 bg-yellow-500/10 px-2.5 py-2">
          {guide.notes.map((n) => (
            <p key={n} className="flex items-start gap-1.5 text-[11px] leading-4 text-yellow-700">
              <Sparkles className="mt-0.5 h-3 w-3 shrink-0" />
              <span>{n}</span>
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 面板顶部的「自动扫描」区：一处触发、一屏结论、一键采用。
 *
 * 顺序上刻意把「扫描」放在最前：用户打开这个面板时大概率不知道路径在哪，
 * 先让机器去找，比先摆三个空选择器有用得多。
 */
export function ScanHub({
  missing,
  scanning,
  scanResult,
  scanError,
  onScan,
  onApplyAll,
  applying,
  applied,
}: {
  missing: SetupKind[]
  scanning: boolean
  scanResult: ScanResult | null
  scanError: string
  onScan: () => void
  onApplyAll: () => void
  applying: boolean
  applied: boolean
}) {
  const found = missing.filter((k) => (scanResult?.candidates?.[k] || []).length > 0)
  const stats = scanResult?.stats

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-1.5 text-sm font-medium text-card-foreground">
            <FolderSearch className="h-4 w-4 text-primary" />
            自动扫描本机
          </p>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            {scanning
              ? "正在查找本机已有的模型与引擎（只读，不改动任何文件）…"
              : scanError
                ? `扫描失败：${scanError}`
                : scanResult
                  ? found.length > 0
                    ? `扫到 ${found.length} 项可用资源${stats ? `（检查 ${stats.dirsVisited} 个目录，${(stats.elapsedMs / 1000).toFixed(1)}s${stats.truncated ? "，已达预算上限，可能不全" : ""}）` : ""}`
                    : "本机没扫到可用的模型或引擎 —— 多半还没下载，下面每项都给了下载链接与放置说明。"
                  : "点一下，让我在本机找找看有没有现成的模型目录，省得你翻盘符。"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {found.length > 0 && (
            <button
              type="button"
              disabled={applying}
              onClick={onApplyAll}
              className="flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground transition hover:bg-primary/90 disabled:opacity-50"
            >
              {applying ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Check className="h-3.5 w-3.5" />
              )}
              一键使用推荐（{found.length}）
            </button>
          )}
          <button
            type="button"
            disabled={scanning || applying}
            onClick={onScan}
            className="flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", scanning && "animate-spin")} />
            {scanResult ? "重新扫描" : "开始扫描"}
          </button>
        </div>
      </div>
      {applied && (
        <p className="mt-2 flex items-center gap-1.5 text-[11px] text-primary">
          <Check className="h-3 w-3" /> 已写入推荐位置 —— 重启后端后生效（下方按钮）
        </p>
      )}
    </div>
  )
}

/** 面板底部的说明：模型为什么不在包里 + 找不到时的兜底。 */
export function GuideFooter({ configPath }: { configPath: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-border bg-background/60 p-3">
      <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
      <span className="text-[11px] leading-4 text-muted-foreground">
        模型与引擎不随安装包分发（合计约 20 GB，且各自的许可条款不同）。安装包只带应用本身，
        你在上面任选一种方式指路：自动扫描 / 手选目录 / 按指引下载。
        配置文件位置：<span className="font-mono">{configPath}</span>
      </span>
    </div>
  )
}
