import { useCallback, useEffect, useRef, useState, type ReactNode } from "react"
import { AlertTriangle, Check, FolderOpen, Info, Loader2, RefreshCw, RotateCcw, X } from "lucide-react"
import {
  getSetupGuides,
  getSetupStatus,
  hasSetup,
  hasSetupScan,
  openGuideLink,
  pickSetupDir,
  restartBackend,
  saveSetup,
  scanSetup,
  showSetupConfig,
  type ResourceGuide,
  type ScanCandidate,
  type ScanResult,
  type SetupItem,
  type SetupKind,
  type SetupStatus,
} from "@/lib/electron"
import { DiscoveryList, GuideBlock, GuideFooter, ScanHub } from "@/components/ModelDiscovery"
import { notify } from "@/lib/notify"
import { cn } from "@/lib/utils"

/** 每一项给用户的说明与"没配会怎样" */
const KIND_META: Record<SetupKind, { hint: string; impact: string }> = {
  tts_models: {
    hint: "选包含 qwen3-tts-1.7b-base 与 qwen3-tts-tokenizer-12hz 两个子目录的 tts_models 目录。",
    impact: "文字转语音、音色微调、微信一键发送不可用。",
  },
  tts_venv: {
    hint: "TTS 专用虚拟环境的 python.exe（tts_trial\\venv312\\Scripts\\python.exe）。模型目录选对时自动推导。",
    impact: "同「TTS 模型」——两者需同时就绪。",
  },
  rvc_root: {
    hint: "RVC 整合包根目录（含 rvc/ 、logs/ 、tools/ 等子目录）。",
    impact: "实时变声、离线 RVC 变声、音色训练不可用。",
  },
}

const SOURCE_LABEL: Record<SetupItem["source"], string> = {
  config: "已配置",
  env: "环境变量",
  derived: "自动推导",
  none: "未配置",
}

/** 检测项 key → app-config 字段名（与主进程 setup-ipc.cjs 的 CONFIG_KEY 保持一致） */
const PATCH_KEY: Record<SetupKind, "ttsModelsDir" | "ttsVenvPy" | "rvcRoot"> = {
  tts_models: "ttsModelsDir",
  tts_venv: "ttsVenvPy",
  rvc_root: "rvcRoot",
}

/** 扫描结果 → 可直接落盘的配置 patch */
function candidatesToPatch(cands: Partial<Record<SetupKind, ScanCandidate[]>>): Partial<SetupStatus["config"]> {
  const pick: Partial<SetupStatus["config"]> = {}
  for (const kind of Object.keys(PATCH_KEY) as SetupKind[]) {
    const top = (cands[kind] || [])[0]
    if (top) pick[PATCH_KEY[kind]] = top.path
  }
  return pick
}

function StatusRow({
  item,
  busy,
  onPick,
  footer,
}: {
  item: SetupItem
  busy: boolean
  onPick: (kind: SetupKind) => void
  footer?: ReactNode
}) {
  const meta = KIND_META[item.key]
  return (
    <div
      className={cn(
        "rounded-lg border px-3 py-2.5",
        item.ok ? "border-primary/25 bg-primary/5" : "border-yellow-500/40 bg-yellow-500/10",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          {item.ok ? (
            <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          ) : (
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-600" />
          )}
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-sm font-medium text-card-foreground">
              {item.label}
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-[10px] font-normal",
                  item.ok ? "bg-primary/15 text-primary" : "bg-yellow-500/20 text-yellow-700",
                )}
              >
                {item.ok ? SOURCE_LABEL[item.source] : item.source === "none" ? "未配置" : "路径无效"}
              </span>
            </p>
            <p className="mt-1 break-all font-mono text-[11px] leading-4 text-muted-foreground">
              {item.path || "（尚未指定）"}
            </p>
            {!item.ok && (
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {item.reason} · 不可用功能：{meta.impact}
              </p>
            )}
          </div>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => onPick(item.key)}
          className="flex shrink-0 items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
        >
          <FolderOpen className="h-3.5 w-3.5" />
          选择
        </button>
      </div>
      <p className="mt-2 text-[11px] leading-4 text-muted-foreground/80">{meta.hint}</p>
      {footer}
    </div>
  )
}

/** 模型与引擎配置面板：检测三项外部资源，允许用户指定目录并落盘。
 *
 * 干净机器（无 D:\变声 / D:\RVC）上，安装包不含模型，用户必须自己指路。
 * 保存后需重启后端 —— VM_* 只在 spawn 时读取，进程内无法热改。
 */
export function ModelSetupPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState("")
  const [dirty, setDirty] = useState(false)
  // ---- 自动扫描 / 下载指引 ----
  const [guides, setGuides] = useState<ResourceGuide[]>([])
  const [guideVerifiedAt, setGuideVerifiedAt] = useState<string>("")
  const [scanning, setScanning] = useState(false)
  const [scanResult, setScanResult] = useState<ScanResult | null>(null)
  const [scanError, setScanError] = useState("")
  const [applying, setApplying] = useState(false)
  const [applied, setApplied] = useState(false)
  const [openGuide, setOpenGuide] = useState<SetupKind | null>(null)
  // 只在面板「打开」时自动扫一次；用户手动点「重新扫描」不受此限
  const autoScannedRef = useRef(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setStatus(await getSetupStatus())
    } finally {
      setLoading(false)
    }
  }, [])

  const runScan = useCallback(async (kinds?: SetupKind[]) => {
    setScanning(true)
    setScanError("")
    try {
      const r = await scanSetup(kinds)
      if (!r) {
        setScanError("当前环境不支持自动扫描")
        setScanResult(null)
        return
      }
      if (!r.ok) {
        setScanError(r.reason || "扫描失败")
        setScanResult(r)
        return
      }
      setScanResult(r)
    } finally {
      setScanning(false)
    }
  }, [])

  useEffect(() => {
    if (!open) {
      autoScannedRef.current = false
      setOpenGuide(null)
      setApplied(false)
      return
    }
    setMsg("")
    setDirty(false)
    autoScannedRef.current = false
    void load()
    if (hasSetupScan) {
      void (async () => {
        const g = await getSetupGuides()
        if (g) {
          setGuides(g.guides)
          setGuideVerifiedAt(g.verifiedAt)
        }
      })()
    }
  }, [open, load])

  // 自动扫描：打开面板且「有缺失项」时自动跑一次。
  // 这是本面板存在的第一性问题 —— 用户打不开功能时，最需要的是"你机器上其实有，
  // 就在这儿"，而不是一个空的目录选择器。
  useEffect(() => {
    if (!open || !status || autoScannedRef.current || !hasSetupScan) return
    if (status.missing.length === 0) return
    autoScannedRef.current = true
    void runScan(status.missing)
  }, [open, status, runScan])

  const candidatesOf = useCallback(
    (kind: SetupKind): ScanCandidate[] => scanResult?.candidates?.[kind] || [],
    [scanResult],
  )

  /** 采用某个候选（单个） */
  const handleUseCandidate = useCallback(async (kind: SetupKind, cand: ScanCandidate) => {
    setBusy(true)
    setMsg("")
    setApplied(false)
    try {
      const next = await saveSetup({ [PATCH_KEY[kind]]: cand.path })
      if (next) {
        setStatus(next)
        setDirty(true)
        setApplied(true)
        notify.success("已采用该位置", "重启后端后生效")
        window.dispatchEvent(new CustomEvent("vm-setup-changed"))
      } else {
        notify.error("保存失败", "配置文件可能不可写")
      }
    } finally {
      setBusy(false)
    }
  }, [])

  /** 一键采用所有推荐位置（每项取扫描得分最高的那个） */
  const handleApplyRecommended = useCallback(async () => {
    const patch = candidatesToPatch(scanResult?.candidates || {})
    if (!Object.keys(patch).length) {
      notify.warn("没有可采用的推荐位置")
      return
    }
    setApplying(true)
    setMsg("")
    try {
      const next = await saveSetup(patch)
      if (next) {
        setStatus(next)
        setDirty(true)
        setApplied(true)
        notify.success(`已写入 ${Object.keys(patch).length} 项推荐位置`, "重启后端后生效")
        window.dispatchEvent(new CustomEvent("vm-setup-changed"))
      } else {
        notify.error("保存失败", "配置文件可能不可写")
      }
    } finally {
      setApplying(false)
    }
  }, [scanResult])

  const handleOpenLink = useCallback((kind: SetupKind, index: number) => {
    void (async () => {
      const ok = await openGuideLink(kind, index)
      if (!ok) notify.error("打开链接失败", "可手动复制上面的地址到浏览器")
    })()
  }, [])

  const handlePick = useCallback(async (kind: SetupKind) => {
    setBusy(true)
    setMsg("")
    try {
      const r = await pickSetupDir(kind)
      if (!r || r.canceled || !r.path) return
      const patch =
        kind === "tts_models" ? { ttsModelsDir: r.path }
        : kind === "tts_venv" ? { ttsVenvPy: r.path }
        : { rvcRoot: r.path }
      const next = await saveSetup(patch)
      if (next) {
        setStatus(next)
        setDirty(true)
        setMsg(r.ok === false ? "已保存（该路径未通过校验，功能可能仍不可用）" : "已保存，重启后端后生效")
        window.dispatchEvent(new CustomEvent("vm-setup-changed"))
      }
    } finally {
      setBusy(false)
    }
  }, [])

  const handleRestart = useCallback(async () => {
    setBusy(true)
    setMsg("")
    try {
      const r = await restartBackend()
      if (!r) { setMsg("当前环境无法自动重启后端，请手动重启应用"); return }
      if (r.running) {
        setDirty(false)
        setMsg("后端已重启，配置已生效")
        window.dispatchEvent(new CustomEvent("vm-setup-changed"))
      } else {
        setMsg(r.reason || "后端重启失败，请查看日志")
      }
    } finally {
      setBusy(false)
      void load()
    }
  }, [load])

  const handleReset = useCallback(async () => {
    if (!window.confirm("清空所有模型路径配置？下次启动会重新弹出引导。")) return
    setBusy(true)
    try {
      const r = await saveSetup({ ttsModelsDir: "", ttsVenvPy: "", rvcRoot: "" })
      if (r) { setStatus(r); setDirty(true); setMsg("已清空，重启后端后生效") }
    } finally {
      setBusy(false)
    }
  }, [])

  if (!open) return null

  const missingCount = status ? status.missing.length : 0

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85dvh] w-full max-w-xl flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <FolderOpen className="h-5 w-5 text-primary" />
            <h2 className="text-sm font-semibold text-foreground">模型与引擎配置</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {!hasSetup ? (
            <div className="rounded-lg border border-border bg-background/60 p-4 text-sm text-muted-foreground">
              <p className="flex items-center gap-2 font-medium text-card-foreground">
                <Info className="h-4 w-4 text-primary" /> 当前不是桌面版环境
              </p>
              <p className="mt-2 text-xs leading-5">
                模型路径需要在后端进程的环境变量里指定。请在启动后端前设置
                <code className="mx-1 rounded bg-muted px-1 py-0.5 font-mono">VM_TTS_MODELS_DIR</code>
                <code className="mx-1 rounded bg-muted px-1 py-0.5 font-mono">VM_TTS_VENV_PY</code>
                <code className="mx-1 rounded bg-muted px-1 py-0.5 font-mono">VM_RVC_ROOT</code>
                三个变量，或写入项目根的 <code className="mx-1 rounded bg-muted px-1 py-0.5 font-mono">.env</code>。
              </p>
            </div>
          ) : loading && !status ? (
            <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在检测模型资源…
            </div>
          ) : status ? (
            <div className="space-y-3">
              <div
                className={cn(
                  "rounded-lg border px-3 py-2.5 text-sm",
                  status.allOk
                    ? "border-primary/30 bg-primary/10 text-primary"
                    : "border-yellow-500/40 bg-yellow-500/10 text-yellow-700",
                )}
              >
                {status.allOk
                  ? "✅ 模型与引擎已就绪"
                  : `⚠ 有 ${missingCount} 项未就绪，对应功能暂不可用（其余功能不受影响）`}
              </div>

              {hasSetupScan && (
                <ScanHub
                  missing={status.items.filter((i) => !i.ok).map((i) => i.key)}
                  scanning={scanning}
                  scanResult={scanResult}
                  scanError={scanError}
                  onScan={() => void runScan(status.items.filter((i) => !i.ok).map((i) => i.key))}
                  onApplyAll={() => void handleApplyRecommended()}
                  applying={applying}
                  applied={applied}
                />
              )}

              <div className="space-y-2">
                {status.items.map((item) => {
                  const guide = guides.find((g) => g.key === item.key)
                  return (
                    <StatusRow
                      key={item.key}
                      item={item}
                      busy={busy}
                      onPick={(k) => void handlePick(k)}
                      footer={
                        // 已就绪的项不再劝：它不需要"换个位置"或"下载指引"
                        item.ok || !hasSetupScan ? null : (
                          <DiscoveryList
                            kind={item.key}
                            candidates={candidatesOf(item.key)}
                            scanning={scanning}
                            scanned={scanResult !== null && !scanning}
                            busy={busy}
                            onUse={(c) => void handleUseCandidate(item.key, c)}
                            onOpenGuide={() =>
                              setOpenGuide((k) => (k === item.key ? null : item.key))
                            }
                            guideOpen={openGuide === item.key}
                          >
                            {openGuide === item.key && guide && (
                              <GuideBlock
                                guide={guide}
                                verifiedAt={guideVerifiedAt}
                                onOpenLink={(i) => handleOpenLink(item.key, i)}
                              />
                            )}
                          </DiscoveryList>
                        )
                      }
                    />
                  )
                })}
              </div>

              <GuideFooter configPath={status.configPath} />
              <button
                type="button"
                onClick={() => void showSetupConfig()}
                className="rounded-md border border-border bg-background px-2.5 py-1.5 text-[11px] text-muted-foreground transition hover:text-foreground"
              >
                打开配置文件位置
              </button>

              {msg && (
                <div className="rounded-md border border-border bg-background px-3 py-2 text-xs text-muted-foreground">
                  {msg}
                </div>
              )}
            </div>
          ) : (
            <div className="py-8 text-center text-sm text-muted-foreground">无法读取配置状态。</div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading || busy}
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} /> 重新检测
            </button>
            {hasSetup && (
              <button
                type="button"
                onClick={() => void handleReset()}
                disabled={busy}
                className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
              >
                <RotateCcw className="h-3.5 w-3.5" /> 清空配置
              </button>
            )}
          </div>
          {hasSetup && (
            <button
              type="button"
              onClick={() => void handleRestart()}
              disabled={busy}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition disabled:opacity-50",
                dirty
                  ? "bg-primary text-primary-foreground hover:bg-primary/90"
                  : "border border-border bg-background text-muted-foreground hover:text-foreground",
              )}
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
              保存并重启后端
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/** 顶部降级提示条：缺配置时展示「TTS 未配置 / RVC 未配置」，点击打开配置面板。
 *  用于 TTS、实时变声等功能页顶部，替代"点了按钮才报错"的迟滞反馈。
 *
 *  自轮询：挂在顶层全局位置，不想让 App 层再维护一份状态。窗口聚焦时刷新
 *  （用户可能在配置面板里刚改完），避免显示过期状态。 */
export function SetupBanner({ onOpen, className }: { onOpen: () => void; className?: string }) {
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [dismissed, setDismissed] = useState(false)

  const refresh = useCallback(async () => {
    setStatus(await getSetupStatus())
  }, [])

  useEffect(() => {
    void refresh()
    // 窗口重新获得焦点时刷新（例如刚从配置文件/资源管理器回来）
    const onFocus = () => void refresh()
    window.addEventListener("focus", onFocus)
    // 配置面板保存后广播，让横幅立即更新
    const onChanged = () => void refresh()
    window.addEventListener("vm-setup-changed", onChanged)
    return () => {
      window.removeEventListener("focus", onFocus)
      window.removeEventListener("vm-setup-changed", onChanged)
    }
  }, [refresh])

  if (!status || dismissed || status.allOk) return null

  const ttsBad = status.items.filter((i) => !i.ok && (i.key === "tts_models" || i.key === "tts_venv"))
  const rvcBad = status.items.filter((i) => !i.ok && i.key === "rvc_root")
  const labels: string[] = []
  if (ttsBad.length) labels.push("TTS 未配置")
  if (rvcBad.length) labels.push("RVC 未配置")

  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 border-b border-yellow-500/40 bg-yellow-500/10 px-5 py-2 text-xs text-yellow-700 sm:px-8",
        className,
      )}
    >
      <span className="flex min-w-0 items-center gap-2">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        <span className="truncate">
          <strong className="font-semibold">{labels.join(" · ")}</strong>
          <span className="ml-1 opacity-90">
            —— 缺少 {status.items.filter((i) => !i.ok).map((i) => i.label).join("、")}，相关功能暂不可用，其余功能不受影响。
          </span>
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={onOpen}
          className="rounded-md border border-yellow-600/40 bg-background/60 px-2.5 py-1 text-[11px] font-medium text-yellow-800 transition hover:bg-background"
        >
          去配置
        </button>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label="本次不再提示"
          className="flex h-6 w-6 items-center justify-center rounded-md text-yellow-700/70 transition hover:bg-yellow-500/20 hover:text-yellow-800"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </span>
    </div>
  )
}
