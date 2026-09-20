import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, Layers, Loader2, PowerOff, RefreshCw, RotateCcw, X } from "lucide-react"
import { applyPluginPreset, getPlugins, setPluginEnabled } from "@/api/client"
import type { PluginCatalog, PluginEntry, PluginState } from "@/types"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

/**
 * 能力管理：把 `GET /api/plugins` 的清单摊到界面上，并**真的能开关**（插件化第 6 步）。
 *
 * 为什么要有这一屏：清单做成了 API 但用户看不到 —— 于是「某个能力被关了」在界面上
 * 表现为「那一块功能凭空消失」，没有任何解释。而 `SetupBanner` 已经处理了
 * 「能力**不可用**」（加载失败 / 缺依赖），所以这里只补另一半：**被关掉的**能力去哪了。
 *
 * 三态来自 `plugin_manifest.py`，关键是 `disabled` 与 `broken` **分开**：
 * 用户自己关掉的不该被当成故障来报警，但也不该把原因丢掉 —— 所以被关掉的项
 * 照样把 `reasons` 列出来，好回答「你关的，而且它本来就是坏的」。
 *
 * ★ 开关读 `enabled` 而不是 `state`：被别的启用能力依赖时，用户关了它但后端仍保留
 * （否则依赖方会变砖），此时 `state='disabled'` 而 `enabled=true`。拿 `state` 推会
 * 把「被依赖而保留」显示成关着的。
 *
 * ★ 守卫**只写在后端**：本屏不做「这个能不能关」的预判，点了就发请求，被拒就把
 * 后端的 `detail` 原样显示（「你还被某某依赖着，先关掉它」）。前端再写一遍守卫
 * 等于把判据放两处，清单一改就漂。
 *
 * ★ **重启生效**：router 在后端启动时挂好，本轮不做热插拔。改完必须说清，
 * 否则用户点了开关看不到任何变化，只会以为开关坏了。
 */
const CATEGORY_LABEL: Record<PluginEntry["category"], string> = {
  core: "内核（随包、不可关）",
  sound: "声音能力",
  pet: "桌宠",
  hook: "启动钩子",
}

const CATEGORY_ORDER: PluginEntry["category"][] = ["core", "sound", "pet", "hook"]

const STATE_LABEL: Record<PluginState, string> = {
  ok: "可用",
  broken: "未加载",
  disabled: "已关闭",
}

const STATE_CLASS: Record<PluginState, string> = {
  ok: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  broken: "border-destructive/40 bg-destructive/10 text-destructive",
  disabled: "border-border bg-muted text-muted-foreground",
}

function StateBadge({ state }: { state: PluginState }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        STATE_CLASS[state],
      )}
    >
      {STATE_LABEL[state]}
    </span>
  )
}

/** 开关（role="switch"）。`core` 与忙碌态都不给点，但**不隐藏** —— 让用户看见它存在。 */
function Switch({
  on,
  busy,
  disabled,
  title,
  onToggle,
  label,
}: {
  on: boolean
  busy?: boolean
  disabled?: boolean
  title?: string
  onToggle: () => void
  label: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-busy={busy || undefined}
      aria-disabled={disabled || undefined}
      title={title}
      disabled={busy || disabled}
      onClick={onToggle}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full transition",
        on ? "bg-emerald-500/80" : "bg-muted-foreground/30",
        busy || disabled ? "cursor-not-allowed opacity-60" : "hover:opacity-90",
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all",
          on ? "left-[18px]" : "left-0.5",
        )}
      />
    </button>
  )
}

/**
 * 健康探针的这次结果。
 *
 * 后端**不替我们判断 ok**（两个探针形状完全不同，硬凑一个 `ok` 等于替用户下判断），
 * 所以这里也只把原始快照摊开 —— 这块面板本来就是给排查用的「高级」入口，
 * 看 `ready=false` / `done=false` 比看一个被猜出来的绿勾有用。
 */
function HealthProbe({ probe }: { probe: PluginEntry["healthProbe"] }) {
  if (!probe) return null
  if (!probe.ran) {
    return (
      <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
        探针没跑起来：{probe.error}
      </p>
    )
  }
  const parts = Object.entries(probe.data ?? {})
    // 空值不摊：否则每行都挂着一串 `rva=null · dll=null · reason=`，噪声盖过信号
    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
  if (!parts.length) return null
  return (
    <p className="mt-1 font-mono text-[11px] leading-5 text-muted-foreground">
      实时状态：{parts.join(" · ")}
    </p>
  )
}

/** 一个能力要装什么 —— 三项都空就不占版面 */
function Extras({ p }: { p: PluginEntry }) {
  const py = p.extras?.python ?? []
  const external = p.extras?.external ?? []
  const models = p.extras?.models ?? []
  if (!py.length && !external.length && !models.length) return null
  return (
    <p className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] leading-5 text-muted-foreground">
      {py.length ? (
        <span>
          pip: <code className="font-mono">{py.join(", ")}</code>
        </span>
      ) : null}
      {external.length ? <span>需自备：{external.map((x) => x.label).join("、")}</span> : null}
      {/* 取 label 而不是直接 join —— 元素是对象，直接拼会渲染成 [object Object] */}
      {models.length ? <span>模型：{models.map((x) => x.label).join("、")}</span> : null}
    </p>
  )
}

function CapabilityRow({
  p,
  busy,
  onToggle,
}: {
  p: PluginEntry
  busy?: boolean
  onToggle?: (id: string, on: boolean) => void
}) {
  const on = p.enabled ?? p.state !== "disabled"
  const locked = p.core
  const blocked = p.blockedBy?.length ?? 0
  return (
    <li className="rounded-lg border border-border bg-background/60 px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-card-foreground">
            {p.name}
            <code className="ml-2 font-mono text-[11px] font-normal text-muted-foreground">{p.id}</code>
          </p>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{p.summary}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StateBadge state={p.state} />
          {onToggle ? (
            <Switch
              on={on}
              busy={busy}
              disabled={locked}
              label={`${locked ? "核心能力，不可关闭" : on ? "关闭" : "开启"} ${p.name}`}
              title={
                locked
                  ? "核心能力，不能关闭（关掉它整个界面就没有意义了）"
                  : blocked && on
                    ? `仍被 ${p.blockedBy.join("、")} 依赖；要先关掉它们`
                    : on
                      ? "关闭这个能力（重启后生效）"
                      : "开启这个能力（重启后生效）"
              }
              onToggle={() => onToggle(p.id, !on)}
            />
          ) : null}
        </div>
      </div>

      {p.reasons.length ? (
        <ul className="mt-1.5 space-y-0.5">
          {p.reasons.map((r) => (
            <li key={r} className="font-mono text-[11px] leading-5 text-destructive/90">
              {r}
            </li>
          ))}
        </ul>
      ) : null}

      <Extras p={p} />

      <HealthProbe probe={p.healthProbe ?? null} />

      {p.disableNote ? (
        <p className="mt-1 text-[11px] leading-5 text-muted-foreground/80">{p.disableNote}</p>
      ) : null}

      {blocked ? (
        <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
          被依赖：{p.blockedBy.join("、")}（想关它，先关掉这些）
        </p>
      ) : null}

      {p.requires.length ? (
        <p className="mt-1 text-[11px] leading-5 text-muted-foreground">
          依赖：{p.requires.join("、")}
        </p>
      ) : null}
    </li>
  )
}

function Section({
  title,
  hint,
  icon,
  tone = "muted",
  children,
}: {
  title: string
  hint?: string
  icon?: React.ReactNode
  tone?: "muted" | "warn" | "danger"
  children: React.ReactNode
}) {
  return (
    <section>
      <h3
        className={cn(
          "flex items-center gap-1.5 text-xs font-semibold",
          tone === "danger" ? "text-destructive" : tone === "warn" ? "text-yellow-700" : "text-foreground",
        )}
      >
        {icon}
        {title}
      </h3>
      {hint && <p className="mt-1 text-xs leading-5 text-muted-foreground/80">{hint}</p>}
      <div className="mt-2">{children}</div>
    </section>
  )
}

export function CapabilityPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [data, setData] = useState<PluginCatalog | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState("")
  /** 忙碌的能力 id，或 `"preset:<name>"` */
  const [busy, setBusy] = useState<string | null>(null)
  /** 开关被拒 / 写成功后的提示。写成功也要提示 —— 否则"点了没变化"会被当成坏了。 */
  const [notice, setNotice] = useState<{ tone: "ok" | "warn" | "error"; text: string } | null>(null)
  /** 本次会话里改过 → 必须提示重启，不然用户不知道为什么界面没变 */
  const [dirty, setDirty] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      setData(await getPlugins())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const toggle = useCallback(
    async (id: string, on: boolean) => {
      setBusy(id)
      setNotice(null)
      try {
        await setPluginEnabled(id, on)
        setDirty(true)
        setNotice({
          tone: "ok",
          text: `已${on ? "开启" : "关闭"}「${data?.plugins.find((p) => p.id === id)?.name ?? id}」，重启应用后生效。`,
        })
        await load()
      } catch (e) {
        // 守卫的判据只在后端一处，所以这里**原样**转述它的 detail
        // （409 说的是"你还被某某依赖着"，不能被通用文案吃掉）。
        setNotice({ tone: "error", text: e instanceof Error ? e.message : String(e) })
      } finally {
        setBusy(null)
      }
    },
    [data, load],
  )

  const applyPreset = useCallback(
    async (name: string, label: string) => {
      setBusy(`preset:${name}`)
      setNotice(null)
      try {
        await applyPluginPreset(name)
        setDirty(true)
        setNotice({ tone: "ok", text: `已套用「${label}」套餐，重启应用后生效。` })
        await load()
      } catch (e) {
        setNotice({ tone: "error", text: e instanceof Error ? e.message : String(e) })
      } finally {
        setBusy(null)
      }
    },
    [load],
  )

  if (!open) return null

  const plugins = data?.plugins ?? []
  const counts = data?.counts
  const presets = data?.presets ?? []
  const disabled = plugins.filter((p) => p.state === "disabled")
  const broken = plugins.filter((p) => p.state === "broken")
  const usable = plugins.filter((p) => p.state === "ok")
  const groups = CATEGORY_ORDER.map((c) => [c, usable.filter((p) => p.category === c)] as const).filter(
    ([, list]) => list.length > 0,
  )

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="能力管理"
    >
      <div className="max-h-[85vh] w-full max-w-3xl overflow-y-auto rounded-2xl border border-border bg-card p-5 shadow-2xl sm:p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold text-card-foreground">
              <Layers className="h-4 w-4" />
              能力管理
            </h2>
            <p className="mt-1 text-xs leading-5 text-muted-foreground">
              {counts ? (
                <>
                  {counts.total} 项能力 · {counts.ok} 可用
                  {counts.broken ? ` · ${counts.broken} 未加载` : ""}
                  {counts.disabled ? ` · ${counts.disabled} 已关闭` : ""}
                </>
              ) : (
                "正在读取能力清单…"
              )}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              title="重新读取"
              aria-label="重新读取"
              className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {error ? (
          <div className="mt-4">
            <ErrorPanel title="读取能力清单失败" detail={error} hint="后端可能刚启动，稍后重试即可。" />
          </div>
        ) : null}

        {notice ? (
          <p
            role="status"
            className={cn(
              "mt-4 rounded-lg border px-3 py-2 text-xs leading-5",
              notice.tone === "error"
                ? "border-destructive/40 bg-destructive/10 text-destructive"
                : notice.tone === "warn"
                  ? "border-yellow-500/40 bg-yellow-500/10 text-yellow-700"
                  : "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
            )}
          >
            {notice.text}
          </p>
        ) : null}

        {dirty ? (
          <p
            role="status"
            className="mt-2 flex items-start gap-1.5 rounded-lg border border-border bg-muted/60 px-3 py-2 text-xs leading-5 text-muted-foreground"
          >
            <RotateCcw className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              改动已保存，但<strong className="font-medium">要重启应用才生效</strong> —— 路由是后端
              启动时挂好的，本轮不做热插拔。重启后关掉的能力不会加载，也就不占显存。
            </span>
          </p>
        ) : null}

        <div className="mt-5 space-y-6">
          <Section
            title="套餐预设"
            hint="先按套餐选，不够再往下逐项调 —— 直接甩一张裸插件表是配置负担，不是自由度。"
          >
            <div className="flex flex-wrap gap-2">
              {presets.map((preset) => {
                const active = data?.preset === preset.id
                const isBusy = busy === `preset:${preset.id}`
                return (
                  <button
                    key={preset.id}
                    type="button"
                    aria-pressed={active}
                    disabled={busy !== null}
                    onClick={() => void applyPreset(preset.id, preset.label)}
                    className={cn(
                      "flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition disabled:opacity-60",
                      active
                        ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                        : "border-border bg-background/60 text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {isBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                    {preset.label}
                    <span className="font-mono text-[10px] opacity-70">{preset.plugins.length}</span>
                  </button>
                )
              })}
              {data && data.preset === "custom" ? (
                <span className="self-center text-[11px] text-muted-foreground">
                  当前是自定义组合（在套餐之外逐项改过）
                </span>
              ) : null}
            </div>
          </Section>

          <Section
            title="被关掉的能力"
            hint="关掉的能力不会加载 —— 不占显存、不注册路由、界面上也不出现。这里让「功能去哪了」有个答案。"
            icon={<PowerOff className="h-3.5 w-3.5" />}
          >
            {disabled.length ? (
              <ul className="space-y-2">
                {disabled.map((p) => (
                  <CapabilityRow key={p.id} p={p} busy={busy === p.id} onToggle={toggle} />
                ))}
              </ul>
            ) : (
              <p className="rounded-lg border border-dashed border-border px-3 py-2.5 text-xs text-muted-foreground">
                没有被关闭的能力。
              </p>
            )}
          </Section>

          {broken.length ? (
            <Section
              title={`未加载的能力（${broken.length}）`}
              hint="清单里声明了、但启动时没挂上 —— 通常是缺依赖或没配好，装好重启即可。"
              icon={<AlertTriangle className="h-3.5 w-3.5" />}
              tone="danger"
            >
              <ul className="space-y-2">
                {broken.map((p) => (
                  <CapabilityRow key={p.id} p={p} busy={busy === p.id} onToggle={toggle} />
                ))}
              </ul>
            </Section>
          ) : null}

          <Section
            title={`可用能力（${usable.length}）`}
            hint={
              data?.loaders
                ? `加载器账本：${data.loaders.routers} 个路由模块，已挂 ${data.loaders.loaded} 个。`
                : undefined
            }
          >
            <div className="space-y-4">
              {groups.map(([cat, list]) => (
                <div key={cat}>
                  <h4 className="mb-1.5 text-[11px] font-medium text-muted-foreground">
                    {CATEGORY_LABEL[cat]} · {list.length}
                  </h4>
                  <ul className="space-y-2">
                    {list.map((p) => (
                      <CapabilityRow key={p.id} p={p} busy={busy === p.id} onToggle={toggle} />
                    ))}
                  </ul>
                </div>
              ))}
              {!groups.length && !loading ? (
                <p className="rounded-lg border border-dashed border-border px-3 py-2.5 text-xs text-muted-foreground">
                  没有可用的能力。
                </p>
              ) : null}
            </div>
          </Section>
        </div>

        <p className="mt-5 border-t border-border pt-3 text-[11px] leading-5 text-muted-foreground">
          关掉一个能力 = 后端不加载它的路由与启动钩子（不占显存）+ 界面上不出现 + 下次装依赖时跳过它的
          重包。改动<strong className="font-medium">重启应用后生效</strong>。
        </p>
      </div>
    </div>
  )
}
