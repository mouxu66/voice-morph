import { useState } from "react"
import { Bot, CircleAlert, Pause, Play, RefreshCw, Send, Square, Trash2 } from "lucide-react"

import { Card, PageShell, Section } from "@/components/layout/PageShell"
import { cn } from "@/lib/utils"

import type { AutopilotConfig, AutopilotLogEntry, AutopilotSession, AutopilotState } from "@/api/client"
import { canStart, useAutopilot, withChat, withGroup } from "./useAutopilot"

const STATUS_LABEL: Record<string, string> = {
  off: "未开启",
  running: "托管中",
  paused: "已暂停",
  stopping: "正在停止",
}

const ACTION_STYLE: Record<string, string> = {
  sent: "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  test: "border-sky-500/40 bg-sky-500/10 text-sky-600 dark:text-sky-400",
  skip: "border-border bg-background/60 text-muted-foreground",
  error: "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400",
}

function Pill({ children, tone = "flat" }: { children: React.ReactNode; tone?: "flat" | "live" | "warn" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs",
        tone === "flat" && "border-border text-muted-foreground",
        tone === "live" && "border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
        tone === "warn" && "border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400",
      )}
    >
      {children}
    </span>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-foreground">{label}</span>
      {children}
      {hint && <span className="text-[11px] leading-4 text-muted-foreground">{hint}</span>}
    </label>
  )
}

const inputCls =
  "rounded-xl border border-border bg-background/60 px-3 py-2 text-sm text-foreground outline-none transition focus:border-primary/50 disabled:opacity-50"

function SessionRow({
  s,
  editable,
  onToggle,
  onGroup,
}: {
  s: AutopilotSession
  editable: boolean
  onToggle: () => void
  onGroup: () => void
}) {
  return (
    <div className="flex items-center gap-3 border-b border-border/60 px-3 py-2 last:border-0">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-foreground">{s.chat || "（未命名会话）"}</span>
          {s.unread > 0 && <Pill tone="warn">{s.unread} 条未读</Pill>}
          {s.pinned && <Pill>置顶</Pill>}
          {s.muted && <Pill>免打扰</Pill>}
          {s.group && <Pill>群聊</Pill>}
        </div>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          {s.time ? `${s.time} · ` : ""}
          {s.preview || "（没有预览）"}
        </p>
      </div>
      {s.whitelisted && (
        <button
          type="button"
          disabled={!editable}
          onClick={onGroup}
          className={cn(
            "rounded-lg border px-2 py-1 text-xs transition disabled:opacity-40",
            s.group
              ? "border-primary/40 bg-primary/10 text-primary"
              : "border-border text-muted-foreground hover:border-input",
          )}
        >
          {s.group ? "是群聊" : "标记群聊"}
        </button>
      )}
      <button
        type="button"
        disabled={!editable}
        onClick={onToggle}
        className={cn(
          "rounded-lg border px-2.5 py-1 text-xs transition disabled:opacity-40",
          s.whitelisted
            ? "border-red-500/40 text-red-600 hover:bg-red-500/10 dark:text-red-400"
            : "border-primary/40 bg-primary/10 text-primary hover:bg-primary/15",
        )}
      >
        {s.whitelisted ? "移出" : "加入"}
      </button>
    </div>
  )
}

function LogRow({ e }: { e: AutopilotLogEntry }) {
  return (
    <div className="flex flex-wrap items-baseline gap-2 border-b border-border/60 px-3 py-2 text-xs last:border-0">
      <span className="font-mono text-muted-foreground">{e.time || "—"}</span>
      <span className={cn("rounded-full border px-2 py-0.5", ACTION_STYLE[e.action] ?? ACTION_STYLE.skip)}>
        {e.action}
      </span>
      <span className="text-foreground">{e.chat || "—"}</span>
      {e.reply && <span className="text-foreground/80">「{e.reply}」</span>}
      {e.reason && <span className="text-muted-foreground">{e.reason}</span>}
      {e.method && <span className="text-muted-foreground">[{e.method}]</span>}
    </div>
  )
}

export function AutopilotPage() {
  const a = useAutopilot()
  const [testText, setTestText] = useState("这是一条托管链路测试")
  const cfg = a.draft
  const editable = a.state?.status === "off"
  const stats = a.state?.stats

  const setCfg = (next: AutopilotConfig) => a.patch(next)

  return (
    <PageShell>
      <header className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="mb-1 flex items-center gap-2 text-xs font-medium text-primary">
            <span className="h-3 w-0.5 rounded-full bg-primary/70" aria-hidden="true" />
            桌面 · 微信
          </p>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-foreground">
            <Bot className="h-6 w-6 text-primary" />
            全局托管
          </h1>
          <p className="mt-1.5 max-w-3xl text-sm leading-6 text-muted-foreground">
            授权后它会自己盯白名单会话，只在「对方最后发言 + 是提问或 @你」时，用本地模型写一条短句、以
            <span className="text-foreground"> 纯文本 </span>
            发出去。默认关，每次启动都要重新授权；随时可以一键急停。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Pill tone={a.running ? "live" : a.paused ? "warn" : "flat"}>{STATUS_LABEL[a.state?.status ?? "off"]}</Pill>
          {stats && (
            <Pill>
              已发 {stats.sent} · 跳过 {stats.skipped} · 轮次 {stats.ticks}
            </Pill>
          )}
          <button
            type="button"
            onClick={() => void a.load()}
            className="rounded-xl border border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:border-input"
          >
            <RefreshCw className="mr-1 inline h-3.5 w-3.5" />
            刷新
          </button>
        </div>
      </header>

      <Card tone="accent" className="mb-6 p-4">
        <div className="flex gap-3">
          <CircleAlert className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" />
          <div className="min-w-0 text-sm leading-6">
            <p className="font-medium text-foreground">这会替你发消息，不是出草稿</p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted-foreground">
              <li>只发文字，不发语音/文件/图片/红包/转账/链接；不点「+」面板，不覆盖你输入框里的草稿。</li>
              <li>只在你挑的白名单会话里动作；群聊默认要求 @你；免打扰会话跳过。</li>
              <li>判断「最后一条是谁发的」靠像素雷达（UIA 拿不到发送方），偶尔会因窗口被遮挡而判不出 —— 判不出就不发。</li>
              <li>模型没给出合格短句时不发（不会退回「收到」「哈哈哈」这类模板）。</li>
              <li>微信本身有风控；频繁自动回复有被限制的风险，建议低频、少会话。</li>
            </ul>
            <label className="mt-3 flex items-start gap-2 text-foreground">
              <input
                type="checkbox"
                checked={a.consent}
                onChange={(e) => a.setConsent(e.target.checked)}
                disabled={a.state?.status !== "off"}
                className="mt-1 h-4 w-4 accent-[var(--primary)]"
              />
              <span>我知道它会以我的身份自动发送消息，并同意在本次运行期间授权它这样做。</span>
            </label>
          </div>
        </div>
      </Card>

      <div className="mb-6 flex flex-wrap items-center gap-2">
        {a.state?.status === "off" && (
          <button
            type="button"
            disabled={!canStart(a.state, a.consent, cfg) || a.busy === "start"}
            onClick={() => void a.start()}
            className="rounded-xl bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Play className="mr-1.5 inline h-4 w-4" />
            开始托管
          </button>
        )}
        {a.running && (
          <button
            type="button"
            onClick={() => void a.pause()}
            disabled={a.busy === "pause"}
            className="rounded-xl border border-border px-4 py-2 text-sm text-foreground transition hover:border-input"
          >
            <Pause className="mr-1.5 inline h-4 w-4" />
            暂停
          </button>
        )}
        {a.paused && (
          <button
            type="button"
            onClick={() => void a.resume()}
            disabled={a.busy === "resume"}
            className="rounded-xl border border-primary/40 bg-primary/10 px-4 py-2 text-sm text-primary transition"
          >
            <Play className="mr-1.5 inline h-4 w-4" />
            继续
          </button>
        )}
        {a.state?.status !== "off" && (
          <button
            type="button"
            onClick={() => void a.stop()}
            disabled={a.busy === "stop"}
            className="rounded-xl border border-red-500/40 px-4 py-2 text-sm text-red-600 transition hover:bg-red-500/10 dark:text-red-400"
          >
            <Square className="mr-1.5 inline h-4 w-4" />
            急停
          </button>
        )}
        {!a.consent && a.state?.status === "off" && (
          <span className="text-xs text-muted-foreground">先勾选上面的授权，才能开始。</span>
        )}
        {cfg && cfg.whitelist.length === 0 && (
          <span className="text-xs text-muted-foreground">白名单还是空的：在下面挑至少一个会话。</span>
        )}
        {a.state?.last_error && (
          <span className="text-xs text-red-600 dark:text-red-400">最近错误：{a.state.last_error}</span>
        )}
      </div>

      {a.error && (
        <Card className="mb-4 border-red-500/40 p-3 text-sm text-red-600 dark:text-red-400">{a.error}</Card>
      )}
      {a.notice && <Card className="mb-4 border-emerald-500/40 p-3 text-sm text-emerald-600 dark:text-emerald-400">{a.notice}</Card>}

      <Section
        eyebrow="01"
        title="白名单"
        desc="只有这些会话会被托管。群聊请额外标记 —— 群里必须 @你 才会回。"
        actions={
          <button
            type="button"
            onClick={() => void a.load()}
            className="rounded-lg border border-border px-2.5 py-1 text-xs text-muted-foreground transition hover:border-input"
          >
            重新读会话
          </button>
        }
      >
        <Card className="overflow-hidden">
          {a.sessionsError && <p className="px-3 py-3 text-sm text-amber-600 dark:text-amber-400">{a.sessionsError}</p>}
          {!a.sessionsError && a.sessions.length === 0 && (
            <p className="px-3 py-3 text-sm text-muted-foreground">会话列表是空的：微信最小化或没打开？</p>
          )}
          {a.sessions.map((s) => (
            <SessionRow
              key={s.chat}
              s={s}
              editable={editable}
              onToggle={() => cfg && setCfg(withChat(cfg, s.chat, !s.whitelisted))}
              onGroup={() => cfg && setCfg(withGroup(cfg, s.chat, !s.group))}
            />
          ))}
        </Card>
      </Section>

      <Section eyebrow="02" title="回复策略" desc="这些规则决定「什么情况下它才敢替你说话」。运行中不可修改。">
        {cfg && (
          <Card className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="群聊里 @ 我的昵称" hint="多个用逗号分隔；微信 @ 后面带特殊空格，已做归一">
              <input
                className={inputCls}
                disabled={!editable}
                value={cfg.me_names.join(",")}
                onChange={(e) => setCfg({ ...cfg, me_names: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
                placeholder="例如：小明"
              />
            </Field>
            <Field label="静默时段（起 - 止）" hint="跨零点如 23-7；两个相同 = 不启用">
              <div className="flex items-center gap-2">
                {([0, 1] as const).map((i) => (
                  <input
                    key={i}
                    type="number"
                    min={0}
                    max={23}
                    className={cn(inputCls, "w-20")}
                    disabled={!editable}
                    value={cfg.quiet_hours[i]}
                    onChange={(e) => {
                      const qh: [number, number] = [...cfg.quiet_hours] as [number, number]
                      qh[i] = Math.max(0, Math.min(23, Number(e.target.value) || 0))
                      setCfg({ ...cfg, quiet_hours: qh })
                    }}
                  />
                ))}
              </div>
            </Field>
            <Field label="轮询间隔（秒）" hint="5 ~ 600">
              <input
                type="number"
                className={inputCls}
                disabled={!editable}
                value={cfg.poll_s}
                onChange={(e) => setCfg({ ...cfg, poll_s: Number(e.target.value) || 20 })}
              />
            </Field>
            <Field label="同会话最小间隔（秒）" hint="两次自动回复之间的冷却">
              <input
                type="number"
                className={inputCls}
                disabled={!editable}
                value={cfg.min_gap_s}
                onChange={(e) => setCfg({ ...cfg, min_gap_s: Number(e.target.value) || 0 })}
              />
            </Field>
            <Field label="单会话每小时上限" hint="1 ~ 20">
              <input
                type="number"
                className={inputCls}
                disabled={!editable}
                value={cfg.max_per_chat_per_hour}
                onChange={(e) => setCfg({ ...cfg, max_per_chat_per_hour: Number(e.target.value) || 3 })}
              />
            </Field>
            <Field label="本次运行总上限" hint="1 ~ 200，防止一次开太久">
              <input
                type="number"
                className={inputCls}
                disabled={!editable}
                value={cfg.max_per_run}
                onChange={(e) => setCfg({ ...cfg, max_per_run: Number(e.target.value) || 20 })}
              />
            </Field>
            <Field label="读上下文翻页" hint="0 = 不翻（翻页会抢一次前台）；1~3">
              <input
                type="number"
                className={inputCls}
                disabled={!editable}
                value={cfg.history_pages}
                onChange={(e) => setCfg({ ...cfg, history_pages: Number(e.target.value) || 0 })}
              />
            </Field>
            <Field label="回复最大字数" hint="超过就丢弃不发；10 ~ 40">
              <input
                type="number"
                className={inputCls}
                disabled={!editable}
                value={cfg.reply_max_chars}
                onChange={(e) => setCfg({ ...cfg, reply_max_chars: Number(e.target.value) || 30 })}
              />
            </Field>
            <div className="flex flex-col justify-center gap-2">
              <label className="flex items-center gap-2 text-sm text-foreground">
                <input
                  type="checkbox"
                  checked={cfg.question_only}
                  disabled={!editable}
                  onChange={(e) => setCfg({ ...cfg, question_only: e.target.checked })}
                  className="h-4 w-4 accent-[var(--primary)]"
                />
                只回提问或 @我
              </label>
              <label className="flex items-center gap-2 text-sm text-foreground">
                <input
                  type="checkbox"
                  checked={cfg.skip_muted}
                  disabled={!editable}
                  onChange={(e) => setCfg({ ...cfg, skip_muted: e.target.checked })}
                  className="h-4 w-4 accent-[var(--primary)]"
                />
                跳过免打扰会话
              </label>
            </div>
            <div className="flex items-end gap-2">
              <button
                type="button"
                disabled={!editable || a.busy === "save"}
                onClick={() => void a.save()}
                className="rounded-xl bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition disabled:opacity-40"
              >
                保存配置
              </button>
              {!editable && <span className="text-xs text-muted-foreground">运行中不可改</span>}
            </div>
          </Card>
        )}
      </Section>

      <Section
        eyebrow="03"
        title="发送测试"
        desc="只在「文件传输助手」发一条，验证 ValuePattern / 发送按钮 / 会话切换这条链路。不会用到白名单。"
      >
        <Card className="flex flex-wrap items-center gap-2 p-4">
          <input
            className={cn(inputCls, "min-w-[240px] flex-1")}
            value={testText}
            onChange={(e) => setTestText(e.target.value.slice(0, 40))}
            placeholder="测试文本（最长 40 字）"
          />
          <button
            type="button"
            disabled={a.busy === "test" || !testText.trim()}
            onClick={() => void a.testSend(testText.trim())}
            className="rounded-xl border border-primary/40 bg-primary/10 px-4 py-2 text-sm text-primary transition disabled:opacity-40"
          >
            <Send className="mr-1.5 inline h-4 w-4" />
            发送测试
          </button>
        </Card>
      </Section>

      <Section eyebrow="04" title="托管日志" desc="最近 40 条：发了什么、跳过了什么、为什么。">
        <Card className="overflow-hidden">
          {(a.state?.recent ?? []).length === 0 && (
            <p className="px-3 py-3 text-sm text-muted-foreground">还没有记录。</p>
          )}
          {(a.state?.recent ?? []).map((e, i) => (
            <LogRow key={`${e.ts}-${i}`} e={e} />
          ))}
        </Card>
        <p className="mt-2 flex items-center gap-1 text-xs text-muted-foreground">
          <Trash2 className="h-3.5 w-3.5" />
          日志写在 outputs/autopilot_log.jsonl（超过 512KB 自动轮转一份）。
        </p>
      </Section>

      <AutopilotDeps state={a.state} />
    </PageShell>
  )
}

function AutopilotDeps({ state }: { state: AutopilotState | null }) {
  const wechat = state?.wechat
  const advisor = state?.advisor
  return (
    <Section eyebrow="05" title="依赖状态" desc="两个依赖任一不满足，托管都不会发消息（只会记 skip）。">
      <div className="grid gap-3 sm:grid-cols-2">
        <Card className="p-4">
          <p className="text-sm font-medium text-foreground">微信 UIA</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {wechat?.ready ? "就绪" : `未就绪：${String(wechat?.reason || "微信没开或最小化")}`}
          </p>
        </Card>
        <Card className="p-4">
          <p className="text-sm font-medium text-foreground">本地回复模型</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {advisor?.ok ? "就绪（Qwen3-VL-4B）" : `未就绪：${String(advisor?.reason || "模型或 llama-server 不在位")}`}
          </p>
        </Card>
      </div>
    </Section>
  )
}
