import { useCallback, useEffect, useState } from "react"
import type { Mic} from "lucide-react";
import {
  Cable, Check, ChevronDown, Gamepad2, Headphones, Loader2, MessageCircle,
  RefreshCw, ScanLine, TriangleAlert, Wrench, X,
} from "lucide-react"
import { applyAudioConfig, restoreAudioConfig, sendChainCheck } from "@/api/client"
import type { DiagnoseItem } from "@/types"
import { ErrorPanel } from "@/components/ErrorPanel"

/** 分软件麦克风设置指引：发送链路自检的图文步骤（用户最卡的一步，竞品全靠帮助文档自救）。 */
const APP_GUIDES: { app: string; icon: typeof Mic; steps: string[] }[] = [
  {
    app: "微信（通话 / 语音消息）",
    icon: MessageCircle,
    steps: [
      "微信左下角「更多」≡ → 设置 → 音视频通话",
      "把「麦克风」选为 CABLE Output (VB-Audio Virtual Cable)",
      "若用 PC 端「发送到微信」自动发送，则无需改这里（后端会自动切换设备）",
    ],
  },
  {
    app: "QQ（语音通话）",
    icon: MessageCircle,
    steps: [
      "QQ 主面板左下角 ≡ → 设置 → 音视频通话",
      "「麦克风」下拉选 CABLE Output (VB-Audio Virtual Cable)",
      "对方听到变声即成功；想变回来选回「默认设备」或真实麦克风",
    ],
  },
  {
    app: "Discord / YY 等语音房",
    icon: Headphones,
    steps: [
      "用户设置 → 语音和视频 → 输入设备",
      "选 CABLE Output (VB-Audio Virtual Cable)",
      "建议关闭「自动灵敏度」改为手动，避免变声后音量忽大忽小",
    ],
  },
  {
    app: "游戏开黑（无畏契约 / 永劫等）",
    icon: Gamepad2,
    steps: [
      "游戏内 设置 → 音频 → 语音聊天 输入设备",
      "选 CABLE Output (VB-Audio Virtual Cable)（有内置音频设置的游戏）",
      "没有内置设置的游戏：把系统默认录音切到 CABLE Output（本面板「一键最优」）即可",
    ],
  },
  {
    app: "OBS / 直播伴侣",
    icon: Headphones,
    steps: [
      "设置 → 音频 → 全局音频设备 → 麦克风/辅助音频",
      "选 CABLE Output (VB-Audio Virtual Cable)",
      "监听仍走真实耳机，观众侧听到变声、自己不串音",
    ],
  },
]

function ChainItem({ item }: { item: DiagnoseItem }) {
  const ok = item.ok
  const warn = !ok && item.warn
  return (
    <div
      className={`flex items-start gap-3 rounded-lg border px-3 py-2.5 text-sm ${
        ok
          ? "border-primary/20 bg-primary/5"
          : warn
            ? "border-yellow-500/40 bg-yellow-500/10"
            : "border-destructive/30 bg-destructive/10"
      }`}
    >
      {ok ? (
        <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
      ) : (
        <TriangleAlert className={`mt-0.5 h-4 w-4 shrink-0 ${warn ? "text-yellow-500" : "text-destructive"}`} />
      )}
      <div className="min-w-0">
        <p className="font-medium text-card-foreground">{item.label}</p>
        <p className="mt-0.5 break-words text-xs text-muted-foreground">{item.detail}</p>
        {!ok && item.hint && <p className="mt-1 break-words text-xs leading-5 text-foreground/80">{item.hint}</p>}
      </div>
    </div>
  )
}

/** 发送链路自检面板（A1，2026-09-09）：一键检查「变声能不能送进微信/QQ/游戏」。
 * - 检查：VB-CABLE 安装、默认录音设备、默认播放设备（防本机静音）、备份残留；
 * - 修复：一键最优（录音→CABLE）/ 恢复默认；均复用既有 /audio/apply|restore；
 * - 指引：微信/QQ/Discord/游戏/OBS 分软件「选哪个麦克风」图文步骤。 */
export function SendChainCheck({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [info, setInfo] = useState<Awaited<ReturnType<typeof sendChainCheck>> | null>(null)
  const [loading, setLoading] = useState(false)
  const [fixing, setFixing] = useState<"apply" | "restore" | null>(null)
  const [fixMsg, setFixMsg] = useState<string | null>(null)
  const [fixErr, setFixErr] = useState<string | null>(null)
  const [guidesOpen, setGuidesOpen] = useState(false)

  const recheck = useCallback(async () => {
    setLoading(true)
    try {
      setInfo(await sendChainCheck())
      return true
    } catch (e) {
      setInfo(null)
      setFixErr(e instanceof Error ? e.message : String(e))
      return false
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) {
      setFixMsg(null)
      setFixErr(null)
      void recheck()
    }
  }, [open, recheck])

  const runFix = useCallback(
    async (kind: "apply" | "restore", done: string) => {
      setFixing(kind)
      setFixMsg(null)
      setFixErr(null)
      try {
        const r = kind === "apply" ? await applyAudioConfig() : await restoreAudioConfig()
        if (!r.ok) {
          setFixErr(r.error || "操作失败")
          return
        }
        setFixMsg(done)
        await recheck()
      } catch (e) {
        setFixErr(e instanceof Error ? e.message : String(e))
      } finally {
        setFixing(null)
      }
    },
    [recheck],
  )

  if (!open) return null

  const failCount = info ? info.items.filter((i) => !i.ok).length : 0

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2">
            <Cable className="h-5 w-5 text-primary" />
            <h2 className="text-sm font-semibold text-foreground">发送链路自检</h2>
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
          {loading && !info ? (
            <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在检查音频链路…
            </div>
          ) : info ? (
            <div className="space-y-3">
              <div
                className={`rounded-lg border px-3 py-2.5 text-sm ${
                  info.all_ok
                    ? "border-primary/30 bg-primary/10 text-primary"
                    : "border-yellow-500/40 bg-yellow-500/10 text-yellow-500"
                }`}
              >
                {info.all_ok
                  ? "✅ 链路就位：变声已能送进微信 / QQ / 游戏"
                  : `有 ${failCount} 项待处理 — 按提示修一下就能把变声送出去`}
              </div>

              <div className="space-y-2">
                {info.items.map((i) => (
                  <ChainItem key={i.key} item={i} />
                ))}
              </div>

              {fixMsg && (
                <p className="rounded-md border border-primary/30 bg-primary/10 px-3 py-2 text-xs text-primary">
                  {fixMsg}
                </p>
              )}
              {fixErr && <ErrorPanel title="操作失败" detail={fixErr} />}

              {/* 修复动作 */}
              <div className="flex flex-wrap gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => void runFix("apply", "已切到变声最优配置（已备份原配置，可随时恢复）")}
                  disabled={fixing !== null}
                  className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-60"
                >
                  {fixing === "apply" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wrench className="h-3.5 w-3.5" />}
                  一键最优（录音 → 变声声卡）
                </button>
                <button
                  type="button"
                  onClick={() => void runFix("restore", "已恢复你原来的默认设备")}
                  disabled={fixing !== null}
                  className="flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
                >
                  {fixing === "restore" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                  恢复默认设备
                </button>
              </div>

              {/* 分软件指引 */}
              <div className="pt-2">
                <button
                  type="button"
                  onClick={() => setGuidesOpen((v) => !v)}
                  className="flex w-full items-center justify-between rounded-lg border border-border bg-background/50 px-3 py-2 text-xs text-muted-foreground transition hover:border-primary hover:text-foreground"
                >
                  <span className="flex items-center gap-2">
                    <ScanLine className="h-3.5 w-3.5 text-primary" />
                    在微信 / QQ / 游戏 / 直播软件里怎么选麦克风？
                  </span>
                  <ChevronDown className={`h-3.5 w-3.5 transition-transform ${guidesOpen ? "rotate-180" : ""}`} />
                </button>
                {guidesOpen && (
                  <div className="mt-2 space-y-2">
                    {APP_GUIDES.map(({ app, icon: Icon, steps }) => (
                      <div key={app} className="rounded-lg border border-border bg-background/40 px-3 py-2.5">
                        <p className="flex items-center gap-1.5 text-xs font-medium text-card-foreground">
                          <Icon className="h-3.5 w-3.5 text-primary" /> {app}
                        </p>
                        <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-xs leading-5 text-muted-foreground">
                          {steps.map((s) => (
                            <li key={s}>{s}</li>
                          ))}
                        </ol>
                      </div>
                    ))}
                    <p className="flex items-center gap-1.5 rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 py-2 text-xs leading-5 text-yellow-500">
                      <Headphones className="h-3.5 w-3.5 shrink-0" />
                      实时变声建议戴耳机：音箱外放会被麦克风再收回去，产生回声。
                    </p>
                  </div>
                )}
              </div>
            </div>
          ) : null}
        </div>

        {/* 底部 */}
        <div className="flex items-center justify-between gap-2 border-t border-border px-5 py-3">
          <button
            type="button"
            onClick={() => void recheck()}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} /> 重新检测
          </button>
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
