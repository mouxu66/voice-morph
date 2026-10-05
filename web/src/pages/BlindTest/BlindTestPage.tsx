import { useMemo, useRef, useState } from "react"
import {
  Check,
  Eye,
  Headphones,
  ListPlus,
  Loader2,
  Lock,
  QrCode,
  Trash2,
  Unlock,
  X,
} from "lucide-react"
import { renderSVG } from "uqr"
import type { useBlindTest } from "@/pages/BlindTest/useBlindTest"
import { auditionRefFromUrl } from "@/pages/BlindTest/useBlindTest"
import { ErrorPanel } from "@/components/ErrorPanel"
import { Card, PageShell, Section } from "@/components/layout/PageShell"
import { pluginVisible, usePluginCatalog } from "@/lib/pluginRoutes"
import { cn } from "@/lib/utils"

/**
 * 「盲听评测」页 —— 主观听测的组织台。
 *
 * ① 发起：样本从上传 / 试音间结果两头来，起个标题建会话（样本匿名化打乱）；
 * ② 分发：二维码 = listen.html?t=<token>，手机（同 Wi-Fi）扫码盲听打 MOS 分；
 * ③ 收卷：汇总排名在这里看 —— label（哪个样本是谁）只有桌面端看得到，
 *    手机端从接口形状上就拿不到（盲听的底线，后端测试钉死）。
 *
 * 二维码复用遥控页同一套：uqr 纯几何生成 SVG + /api/system/lan 的局域网地址。
 */

function QrSvg({ text, size = 148 }: { text: string; size?: number }) {
  const svg = useMemo(() => renderSVG(text, { ecc: "M", border: 4 }), [text])
  return (
    <span
      role="img"
      aria-label="盲听页地址二维码"
      className="block shrink-0 overflow-hidden rounded-lg bg-white p-1 shadow-sm [&>svg]:h-full [&>svg]:w-full"
      style={{ width: size, height: size }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

function timeLabel(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false })
  } catch {
    return String(ts)
  }
}

export function BlindTestPage(p: ReturnType<typeof useBlindTest>) {
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [copied, setCopied] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  // 「从试音间导入」打的是 sound.audition（可关）的端点 —— 试音间关着时这块整个
  // 藏起来，否则按钮点下去就是 404（audit_endpoint_ownership 抓的就是这种裸渲染）。
  const { state } = usePluginCatalog()
  const catalog = state.status === "ready" ? state.catalog : null
  const auditionOn = pluginVisible(catalog, "sound.audition")

  const d = p.detail
  const url = d ? p.listenUrl(d.token) : null

  async function copyUrl() {
    if (!url) return
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* 剪贴板不可用就算了 —— URL 本来就完整显示着 */
    }
  }

  return (
    <div className="min-h-full bg-gradient-to-br from-background via-background to-card">
      <PageShell className="space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0">
            <p className="mb-1.5 flex items-center gap-2 text-xs font-medium text-primary">
              <span className="h-3 w-0.5 rounded-full bg-primary/70" aria-hidden="true" />
              盲听评测
            </p>
            <h1 className="text-xl font-semibold tracking-tight text-foreground">
              让真人评委盲听打分，主观分与客观分互补
            </h1>
            <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted-foreground">
              样本匿名打乱成「样本 A/B/C…」，手机扫码（同一个 Wi-Fi）逐个听、按 1~5 分打分；
              结果回传到这里汇总排名。<strong className="font-medium text-card-foreground">
              哪个样本是谁只有你看得到</strong>，评委的页面从接口上就拿不到这层对应关系。
            </p>
          </div>
        </header>

        {p.error && <ErrorPanel title="操作失败" detail={p.error} />}
        {p.feedback && (
          <div className="flex items-center gap-2 rounded-xl border border-primary/30 bg-primary/5 px-3.5 py-2.5 text-xs text-primary animate-in fade-in slide-in-from-top-2 duration-300">
            <Check className="h-3.5 w-3.5 shrink-0" />
            {p.feedback}
          </div>
        )}

        {/* ① 发起盲听 */}
        <Section
          title="发起盲听"
          desc="样本 2~10 个：本机上传，或直接引用试音间刚跑完的结果。标签是给你自己看的名字，评委看不到。"
        >
          <Card className="p-5 sm:p-6" data-testid="blindtest-create">
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
                <input
                  value={p.title}
                  onChange={(e) => p.setTitle(e.target.value)}
                  placeholder="这场盲听叫什么（可选，默认「未命名盲听」）"
                  maxLength={100}
                  className={cn(
                    "w-full rounded-lg border border-border bg-background px-3 py-2 text-xs",
                    "text-card-foreground placeholder:text-muted-foreground/60",
                    "transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
                  )}
                  aria-label="盲听标题"
                />
                <div className="flex shrink-0 items-center gap-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept="audio/*,.wav,.mp3,.flac,.ogg,.m4a,.aac,.opus,.webm"
                    multiple
                    onChange={(e) => {
                      p.addFiles(Array.from(e.target.files ?? []))
                      e.target.value = ""
                    }}
                    className="hidden"
                  />
                  <button
                    type="button"
                    onClick={() => fileInputRef.current?.click()}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3 py-2",
                      "text-xs text-card-foreground transition hover:border-primary"
                    )}
                  >
                    <ListPlus className="h-3.5 w-3.5 text-primary" />
                    加样本
                  </button>
                  {auditionOn && (
                    <button
                      type="button"
                      onClick={() => void p.openPicks()}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-lg border border-border bg-background px-3 py-2",
                        "text-xs text-card-foreground transition hover:border-primary"
                      )}
                    >
                      <Headphones className="h-3.5 w-3.5 text-primary" />
                      从试音间导入
                    </button>
                  )}
                </div>
              </div>

              {auditionOn && p.picksOpen && (
                <div className="rounded-xl border border-border bg-muted/30 p-3" data-testid="audition-picks">
                  <div className="flex items-center justify-between">
                    <p className="text-xs font-medium text-card-foreground">
                      试音间当前结果（试音间跑完才会有）
                    </p>
                    <button
                      type="button"
                      onClick={() => p.setPicksOpen(false)}
                      className="text-muted-foreground transition hover:text-foreground"
                      aria-label="收起试音间结果"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  {p.picksError && <p className="mt-1.5 text-xs text-destructive">{p.picksError}</p>}
                  {!p.picks && !p.picksError && (
                    <p className="mt-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" /> 读取中…
                    </p>
                  )}
                  {p.picks && p.picks.length === 0 && (
                    <p className="mt-1.5 text-xs text-muted-foreground">
                      试音间还没有完成的结果 —— 先去「试音间」跑一批，或直接上传样本。
                    </p>
                  )}
                  {p.picks && p.picks.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {p.picks.map((pick) => {
                        const ref = auditionRefFromUrl(pick.url)
                        const added = p.drafts.some((dr) => dr.ref === ref)
                        return (
                          <button
                            key={pick.voice_id}
                            type="button"
                            disabled={added || p.drafts.length >= 10}
                            onClick={() => p.addPick(pick)}
                            className={cn(
                              "rounded-full border px-2.5 py-1 text-[11px] transition",
                              added
                                ? "cursor-default border-primary/40 bg-primary/10 text-primary"
                                : "border-border text-card-foreground hover:border-primary hover:text-primary",
                              "disabled:cursor-not-allowed disabled:opacity-60"
                            )}
                          >
                            {added ? "已加入" : pick.display_name}
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
              )}

              {p.drafts.length > 0 && (
                <ul className="space-y-1.5" data-testid="draft-list">
                  {p.drafts.map((dr, i) => (
                    <li key={dr.key} className="flex items-center gap-2">
                      <span className="w-14 shrink-0 font-mono text-[11px] text-muted-foreground">
                        样本{i + 1}
                      </span>
                      <span
                        className="w-32 shrink-0 truncate text-xs text-muted-foreground"
                        title={dr.name}
                      >
                        {dr.kind === "ref" ? "（试音间）" : "（上传）"} {dr.name}
                      </span>
                      <input
                        value={dr.label}
                        onChange={(e) => p.setLabel(dr.key, e.target.value)}
                        maxLength={50}
                        placeholder="标签（评委看不到）"
                        className={cn(
                          "min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 py-1.5 text-xs",
                          "text-card-foreground placeholder:text-muted-foreground/60",
                          "transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
                        )}
                        aria-label={`样本${i + 1}标签`}
                      />
                      <button
                        type="button"
                        onClick={() => p.removeDraft(dr.key)}
                        className="shrink-0 rounded-md p-1 text-muted-foreground transition hover:text-destructive"
                        aria-label={`移除样本${i + 1}`}
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => void p.create()}
                  disabled={p.creating || p.drafts.length < 2}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2",
                    "text-xs font-medium text-primary-foreground transition hover:opacity-90",
                    "disabled:cursor-not-allowed disabled:opacity-50"
                  )}
                >
                  {p.creating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Headphones className="h-3.5 w-3.5" />}
                  {p.creating ? "创建中…" : "创建盲听会话"}
                </button>
                {p.drafts.length < 2 && (
                  <span className="text-[11px] text-muted-foreground">至少要 2 个样本</span>
                )}
              </div>
            </div>
          </Card>
        </Section>

        {/* ② 二维码与结果 */}
        <Section
          title="会话"
          desc="点开一场会话拿二维码；收卷后排名就定了。汇总里有几票、均分、离散度 —— 均分相同时离散小的排前面。"
        >
          <Card className="p-5 sm:p-6" data-testid="blindtest-sessions">
            {p.loading ? (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> 读取会话中…
              </p>
            ) : p.sessions.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                还没有盲听会话 —— 上面加样本、创建一场，几秒钟的事。
              </p>
            ) : (
              <ul className="space-y-1.5">
                {p.sessions.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => p.setSelectedId(s.id === p.selectedId ? "" : s.id)}
                      className={cn(
                        "flex w-full items-center gap-3 rounded-lg border px-3 py-2 text-left transition",
                        s.id === p.selectedId
                          ? "border-primary/50 bg-primary/5"
                          : "border-border hover:border-primary/40"
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate text-sm text-card-foreground">
                        {s.title}
                      </span>
                      <span className="shrink-0 text-[11px] text-muted-foreground">
                        {s.n_samples} 样本 · {s.n_responses} 份打分
                      </span>
                      {s.closed ? (
                        <Lock className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      ) : (
                        <Eye className="h-3.5 w-3.5 shrink-0 text-primary" />
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </Section>

        {d && (
          <Section title={`「${d.title}」`} desc={`创建于 ${timeLabel(d.created_at)} · 样本与身份映射只在桌面端可见`}>
            <Card className="p-5 sm:p-6" data-testid="blindtest-detail">
              <div className="flex flex-col gap-5">
                <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
                  {url ? (
                    <>
                      <QrSvg text={url} />
                      <div className="min-w-0 flex-1 text-xs leading-5 text-muted-foreground">
                        <p>
                          评委与电脑连<strong className="font-medium text-foreground">同一个 Wi-Fi</strong>
                          ，扫码即进盲听页 —— 不用手填地址。
                        </p>
                        <p className="mt-2 break-all rounded-md border border-border bg-background/60 px-2 py-1.5 font-mono text-[11px] text-foreground">
                          {url}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => void copyUrl()}
                            className="rounded-md border border-border px-2 py-1 text-[11px] font-medium text-foreground transition hover:border-primary hover:text-primary"
                          >
                            {copied ? "已复制" : "复制地址"}
                          </button>
                          <button
                            type="button"
                            onClick={() => void p.closeSession(d.id, !d.closed)}
                            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-foreground transition hover:border-primary hover:text-primary"
                          >
                            {d.closed ? <Unlock className="h-3 w-3" /> : <Lock className="h-3 w-3" />}
                            {d.closed ? "重新开放" : "收卷（停止打分）"}
                          </button>
                          {confirmDelete ? (
                            <span className="inline-flex items-center gap-1.5 text-[11px]">
                              <button
                                type="button"
                                onClick={() => void p.removeSession(d.id)}
                                className="rounded-md border border-destructive/50 px-2 py-1 font-medium text-destructive transition hover:bg-destructive/10"
                              >
                                确认删除
                              </button>
                              <button
                                type="button"
                                onClick={() => setConfirmDelete(false)}
                                className="text-muted-foreground hover:text-foreground"
                              >
                                取消
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              onClick={() => setConfirmDelete(true)}
                              className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-destructive hover:text-destructive"
                            >
                              <Trash2 className="h-3 w-3" />
                              删除
                            </button>
                          )}
                        </div>
                      </div>
                    </>
                  ) : (
                    <div className="flex items-start gap-3 rounded-xl border border-border bg-muted/30 p-4 text-xs leading-5 text-muted-foreground">
                      <QrCode className="mt-0.5 h-4 w-4 shrink-0" />
                      没拿到局域网地址（电脑没连网？）—— 连上网络后刷新本页，或先把会话留着。
                    </div>
                  )}
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs" data-testid="blindtest-aggregate">
                    <thead>
                      <tr className="border-b border-border text-muted-foreground">
                        <th className="py-1.5 pr-3 font-medium">#</th>
                        <th className="py-1.5 pr-3 font-medium">样本</th>
                        <th className="py-1.5 pr-3 font-medium">是谁（仅你可见）</th>
                        <th className="py-1.5 pr-3 font-medium">均分</th>
                        <th className="py-1.5 pr-3 font-medium">离散</th>
                        <th className="py-1.5 pr-3 font-medium">票数</th>
                        <th className="py-1.5 font-medium">最低~最高</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.aggregate.map((r) => (
                        <tr key={r.sid} className="border-b border-border/60 last:border-0">
                          <td className="py-1.5 pr-3 font-mono text-muted-foreground">{r.rank ?? "—"}</td>
                          <td className="py-1.5 pr-3 font-mono font-semibold text-foreground">{r.sid}</td>
                          <td className="py-1.5 pr-3 text-card-foreground">{r.label}</td>
                          <td className="py-1.5 pr-3 font-semibold text-primary">{r.mean ?? "—"}</td>
                          <td className="py-1.5 pr-3 text-muted-foreground">{r.std ?? "—"}</td>
                          <td className="py-1.5 pr-3 text-muted-foreground">{r.n}</td>
                          <td className="py-1.5 text-muted-foreground">
                            {r.min !== null ? `${r.min}~${r.max}` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {d.responses.length > 0 && (
                  <details className="text-xs">
                    <summary className="cursor-pointer select-none text-muted-foreground transition hover:text-foreground">
                      逐份打分（{d.responses.length} 份，含评委留言）
                    </summary>
                    <ul className="mt-2 space-y-1.5">
                      {d.responses.map((r, i) => (
                        <li key={i} className="rounded-lg border border-border/60 bg-muted/20 px-2.5 py-1.5">
                          <span className="font-mono text-[11px] text-muted-foreground">
                            {timeLabel(r.at)}
                          </span>
                          <span className="ml-2 font-mono text-card-foreground">
                            {Object.entries(r.scores)
                              .sort(([a], [b]) => a.localeCompare(b))
                              .map(([sid, v]) => `${sid}=${v}`)
                              .join(" ")}
                          </span>
                          {r.note && <span className="ml-2 text-muted-foreground">「{r.note}」</span>}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            </Card>
          </Section>
        )}
      </PageShell>
    </div>
  )
}
