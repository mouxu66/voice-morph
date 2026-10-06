import { useCallback, useEffect, useState } from "react"
import { Bot, Check, Copy, Power, RefreshCw, Server, Stethoscope, X } from "lucide-react"
import { diagnose, getDiagnosticReport, getHealth } from "@/api/client"
import type { DiagnoseInfo } from "@/types"
import { useAppStore } from "@/store/useAppStore"
import { ErrorPanel } from "@/components/ErrorPanel"
import { hasElectron, showBackendLog, startBackend, stopBackend } from "@/lib/electron"

const START_CMD = `# 在含 m2_server/ 的项目根目录执行（先激活虚拟环境）：
.venv\\Scripts\\activate
python m2_server/server.py`

/** 环境体检面板：检查本机依赖并支持一键启动后端。
 * - 后端在线：展示勾叉清单（ffmpeg / RVC 整合包 / 权重 / CUDA / 模型），失败项带排查建议与复制。
 * - 后端离线：Electron 下点「启动本地服务」真正拉起；非桌面端降级为「显示命令 + 复制」。 */
export function EnvHealth({
  open,
  onClose,
  onOpenAi,
}: {
  open: boolean
  onClose: () => void
  /** 打开「AI 调试面」面板（动作目录 + AI 操作记录）。 */
  onOpenAi: () => void
}) {
  const backendUp = useAppStore((s) => s.backendUp)
  const setHealth = useAppStore((s) => s.setHealth)

  const [diag, setDiag] = useState<DiagnoseInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [starting, setStarting] = useState(false)
  const [startErr, setStartErr] = useState<string | null>(null)
  const [showCmd, setShowCmd] = useState(false)
  const [copiedCmd, setCopiedCmd] = useState(false)
  // "" = 未复制 / "full" = 复制了后端 AI 调试接口的完整报告 / "brief" = 降级到本地简版
  const [copiedReport, setCopiedReport] = useState<"" | "full" | "brief">("")

  const recheck = useCallback(async (): Promise<boolean> => {
    setLoading(true)
    try {
      const h = await getHealth()
      setHealth(h)
      if (h) {
        const d = await diagnose()
        setDiag(d)
        return true
      }
      setDiag(null)
      return false
    } catch {
      setHealth(null)
      setDiag(null)
      return false
    } finally {
      setLoading(false)
    }
  }, [setHealth])

  // 打开即体检；后端状态翻转（如启动成功）也自动刷新清单
  useEffect(() => {
    if (open) void recheck()
  }, [open, backendUp, recheck])

  const handleStart = useCallback(async () => {
    setStarting(true)
    setStartErr(null)
    setShowCmd(false)
    try {
      const r = await startBackend()
      if (!r) {
        // 非桌面端（Vite / 局域网）：给命令让用户自己跑
        setShowCmd(true)
        return
      }
      if (r.running) {
        const ok = await recheck()
        if (!ok) setStartErr("后端已尝试启动但仍无法连接，请查看后端日志")
      } else {
        setStartErr(r.reason || "后端启动失败，请查看日志")
      }
    } catch (e) {
      setStartErr(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }, [recheck])

  const handleRestart = useCallback(async () => {
    setStarting(true)
    setStartErr(null)
    try {
      await stopBackend()
      const r = await startBackend()
      if (!r) {
        setShowCmd(true)
        return
      }
      if (r.running) await recheck()
      else setStartErr(r.reason || "重启失败")
    } catch (e) {
      setStartErr(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }, [recheck])

  const copyCmd = async () => {
    try {
      await navigator.clipboard.writeText(START_CMD)
      setCopiedCmd(true)
      setTimeout(() => setCopiedCmd(false), 1500)
    } catch {
      /* 忽略 */
    }
  }

  // 首选：后端的**完整诊断报告**（`GET /api/ai/context?format=md`，已脱敏）——
  // 它比本地拼的简版多出能力加载 / 音频链路 / 预热 / 上一条发送的 warning /
  // 疑似错误日志，正是「这次为什么失败」要用的东西（见 docs/AI调试端点设计.md）。
  // 取不到时降级到本地简版，并**如实标注「简版」**—— 一句「已复制」把两种
  // 内容不同的东西说成同一件事，会让用户以为 AI 看到了完整现场。
  const prepareReportText = async (): Promise<{ text: string; mode: "full" | "brief" } | null> => {
    try {
      return { text: await getDiagnosticReport(), mode: "full" }
    } catch {
      if (!diag) return null
      const lines = diag.items
        .filter((i) => !i.ok)
        .map((i) => `【${i.label}】未通过\n  - 现状：${i.detail}\n  - 排查：${i.hint || "—"}`)
      return {
        text:
          `变声工坊 · 环境体检（${diag.all_ok ? "全部通过" : "存在未通过项"}）\n\n` +
          (lines.length ? lines.join("\n\n") : "（无未通过项）"),
        mode: "brief",
      }
    }
  }

  const copyReport = async () => {
    const prepared = await prepareReportText()
    if (!prepared) return
    try {
      await navigator.clipboard.writeText(prepared.text)
      setCopiedReport(prepared.mode)
      setTimeout(() => setCopiedReport(""), 1500)
    } catch {
      /* 忽略 */
    }
  }

  if (!open) return null

  const failCount = diag ? diag.items.filter((i) => !i.ok).length : 0

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <Stethoscope className="h-5 w-5 text-primary" />
            <h2 className="text-sm font-semibold text-foreground">环境体检</h2>
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

        {/* 内容 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {!backendUp ? (
            <div className="rounded-xl border border-destructive/40 bg-destructive/10 p-4">
              <div className="flex items-center gap-2 text-destructive">
                <Server className="h-5 w-5" />
                <p className="font-semibold">本地推理服务未启动</p>
              </div>
              <p className="mt-2 text-sm leading-5 text-muted-foreground">
                所有功能（TTS / 变声 / 挖掘 / 有声书）都依赖本机 Python 后端。启动后界面会自动恢复。
              </p>

              {hasElectron && !showCmd ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={handleStart}
                    disabled={starting}
                    className="flex items-center gap-2 rounded-lg bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground shadow-sm transition hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-60"
                  >
                    {starting ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Power className="h-4 w-4" />}
                    {starting ? "启动中…" : "启动本地服务"}
                  </button>
                  <button
                    type="button"
                    onClick={() => void showBackendLog()}
                    className="flex items-center gap-2 rounded-lg border border-border bg-background px-3.5 py-2 text-sm text-muted-foreground transition hover:text-foreground"
                  >
                    查看日志
                  </button>
                </div>
              ) : (
                <div className="mt-3">
                  <p className="mb-1 text-xs text-muted-foreground">请手动启动后端（打包桌面版会自动拉起）：</p>
                  <pre className="overflow-auto rounded-lg border border-border bg-background px-3 py-2 font-mono text-xs text-foreground/90">
                    {START_CMD}
                  </pre>
                  <button
                    type="button"
                    onClick={copyCmd}
                    className="mt-2 flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground"
                  >
                    {copiedCmd ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                    {copiedCmd ? "已复制" : "复制命令"}
                  </button>
                </div>
              )}

              {startErr && (
                <div className="mt-3">
                  <ErrorPanel
                    title="启动失败"
                    detail={startErr}
                    hint="点「查看日志」定位根因；或确认 Python 虚拟环境 .venv 已就绪（requirements.txt 依赖已装）。"
                  />
                </div>
              )}
            </div>
          ) : loading && !diag ? (
            <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
              <RefreshCw className="h-4 w-4 animate-spin" /> 正在检查本机环境…
            </div>
          ) : diag ? (
            <div className="space-y-3">
              <div
                className={`rounded-lg border px-3 py-2.5 text-sm ${
                  diag.all_ok
                    ? "border-primary/30 bg-primary/10 text-primary"
                    : "border-yellow-500/40 bg-yellow-500/10 text-yellow-500"
                }`}
              >
                {diag.all_ok
                  ? "✅ 环境就绪，可以开干"
                  : `⚠ 有 ${failCount} 项待处理（不影响基本使用，按需修复）`}
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={copyReport}
                  className="flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground"
                >
                  {copiedReport ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                  {copiedReport === "full"
                    ? "已复制"
                    : copiedReport === "brief"
                      ? "已复制（简版）"
                      : "复制诊断报告"}
                </button>

                {/* AI 调试面：动作目录 + AI 操作记录（第二期）。
                    入口放这里而不是另开一处 —— 用户在报障时的动线就是
                    「体检 → 把报告给 AI」，多一个入口就多一次找不到。
                    **不受 hook.ai 启用状态门控**：关掉写能力后仍要看得到
                    「AI 之前做过什么」，关插件是止血不是销毁证据。 */}
                <button
                  type="button"
                  onClick={onOpenAi}
                  className="flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground"
                >
                  <Bot className="h-3.5 w-3.5" />
                  AI 调试面
                </button>
              </div>

              <div className="space-y-2">
                {diag.items.map((i) =>
                  i.ok ? (
                    <div
                      key={i.key}
                      className="flex items-start gap-3 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2.5 text-sm"
                    >
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                      <div className="min-w-0">
                        <p className="font-medium text-card-foreground">{i.label}</p>
                        <p className="mt-0.5 break-words text-xs text-muted-foreground">{i.detail}</p>
                      </div>
                    </div>
                  ) : (
                    <ErrorPanel key={i.key} title={i.label} detail={i.detail} hint={i.hint ?? null} />
                  ),
                )}
              </div>

              {/* 跳过项必须说出来：否则"体检项变少了"看起来像后端坏了，
                  而真相是"你关掉了相关能力，所以不用查它" */}
              {diag.skipped?.length ? (
                <p className="text-xs leading-5 text-muted-foreground">
                  {diag.skipped.length} 项未检查（相关能力已关闭）：
                  {diag.skipped.map((s) => s.label).join("、")}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>

        {/* 底部操作 */}
        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={() => void recheck()}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> 重新检测
          </button>
          {backendUp && hasElectron && (
            <button
              type="button"
              onClick={() => void handleRestart()}
              disabled={starting}
              className="flex items-center gap-1.5 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              <Power className="h-3.5 w-3.5" /> 重启后端
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:bg-primary/90"
          >
            知道了
          </button>
        </div>
      </div>
    </div>
  )
}
