import { useCallback, useEffect, useState } from "react"
import { AlertTriangle, Layers, Loader2, PowerOff, RefreshCw, X } from "lucide-react"
import { getPlugins } from "@/api/client"
import type { PluginCatalog, PluginEntry, PluginState } from "@/types"
import { cn } from "@/lib/utils"
import { ErrorPanel } from "@/components/ErrorPanel"

/**
 * 能力管理：把 `GET /api/plugins` 的清单摊到界面上。
 *
 * 为什么要有这一屏：步 2 只把清单做成了 API，用户看不到 —— 于是「某个能力被关了」
 * 在界面上表现为「那一块功能凭空消失」，没有任何解释。而 `SetupBanner` 已经处理了
 * 「能力**不可用**」（加载失败 / 缺依赖），所以这里只补另一半：**被关掉的**能力去哪了。
 *
 * 三态来自 `plugin_manifest.py`，关键是 `disabled` 与 `broken` **分开**：
 * 用户自己关掉的不该被当成故障来报警，但也不该把原因丢掉 —— 所以被关掉的项
 * 照样把 `reasons` 列出来，好回答「你关的，而且它本来就是坏的」。
 *
 * 本屏**只读**：开关能力（含套餐预设）是插件化第 6 步，届时这里才变成可操作的。
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

function CapabilityRow({ p }: { p: PluginEntry }) {
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
        <StateBadge state={p.state} />
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

      {p.disableNote ? (
        <p className="mt-1 text-[11px] leading-5 text-muted-foreground/80">{p.disableNote}</p>
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

  if (!open) return null

  const plugins = data?.plugins ?? []
  const counts = data?.counts
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

        <div className="mt-5 space-y-6">
          <Section
            title="被关掉的能力"
            hint="关掉的能力不会加载 —— 不占显存、不注册路由、界面上也不出现。这里让「功能去哪了」有个答案。"
            icon={<PowerOff className="h-3.5 w-3.5" />}
          >
            {disabled.length ? (
              <ul className="space-y-2">
                {disabled.map((p) => (
                  <CapabilityRow key={p.id} p={p} />
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
                  <CapabilityRow key={p.id} p={p} />
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
                      <CapabilityRow key={p.id} p={p} />
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
          当前<strong className="font-medium">只读</strong>：关闭 / 开启能力（含套餐预设与依赖守卫）
          会在插件化的第 6 步接入界面。
        </p>
      </div>
    </div>
  )
}
