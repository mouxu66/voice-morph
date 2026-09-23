import { useCallback, useEffect, useMemo, useState } from "react"
import {
  AlertTriangle,
  ChevronRight,
  Layers,
  Loader2,
  PowerOff,
  RefreshCw,
  RotateCcw,
  X,
} from "lucide-react"
import { applyPluginPreset, getPlugins, setPluginEnabled } from "@/api/client"
import { restartBackend } from "@/lib/electron"
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
 *
 * ---------------------------------------------------------------- 第 7 步：界面语言
 *
 * 第 6 步把「能开关」做出来了，但那一屏是**开发者控制台**：每张卡底下挂着
 * `pip:` / `requires:` / `blockedBy:` / 探针快照共 7 段元信息，而 16/19 项都挂着一个
 * 绿徽章。用户打开这一屏只问三件事 —— **我有什么能力 / 这块功能为什么不见了 /
 * 关掉能省多少**。所以这一步只改「怎么说」，不改后端：
 *
 * 1. **沉默即正常**：`ok` 不再挂徽章。满屏绿徽章会盖住真正的例外，还和 `notice`
 *    的成功色撞。只有例外上色：已关闭（中性）/ 未加载（红）/ **待重启（琥珀）**。
 * 2. **字段折叠**：元信息一条没丢，收进「详情」默认不展开 —— 排查时展开，日常不打扰。
 * 3. **代价与收益同级**：从 `extras[].size_hint_mb` 算出「关掉可省 ≈X GB」，
 *    和依赖提示一样显眼。用户来这一屏的目的恰恰是**省**。
 * 4. **待重启是第四态**：以前只在面板中部插一段灰字，用户点了开关界面毫无变化。
 *    现在改完立刻进 pending 视觉 + 顶部常驻一条「N 项改动待重启 · 立即重启」。
 *
 * ⚠️ 语义色的**三级取值**（底 / 描边 / 字）是分开的，别用 `text-destructive` 一把梭：
 * `destructive` 是固定的 `#ef4444`，压在浅底上当 11px 小字对比度不够。
 * 亮色用 `-600/-700`、暗色用 `-400`，状态条则两主题共用 `-500`（纯色块不叠文字）。
 */
const CATEGORY_LABEL: Record<PluginEntry["category"], string> = {
  core: "内核（随包、不可关）",
  sound: "声音能力",
  pet: "桌宠",
  hook: "启动钩子",
}

const CATEGORY_ORDER: PluginEntry["category"][] = ["core", "sound", "pet", "hook"]

/**
 * 界面上真正要表达的**四态** = 后端三态 + 前端独有的 `pending`。
 *
 * `pending` 不是后端状态：后端只知道「当前启用集是什么」，不知道「你刚改过、但进程还是旧的」。
 * 这一态必须由界面记住，否则「点了开关没变化」就是开关坏了。
 */
type RowState = PluginState | "pending"

const ROW_LABEL: Record<RowState, string> = {
  ok: "可用",
  broken: "未加载",
  disabled: "已关闭",
  pending: "待重启",
}

/**
 * 徽章配色。**`ok` 是空串** —— 沉默即正常，见文件头第 1 条。
 * 只给例外上色，且字色分主题（亮色深一档，压在浅底上才够对比）。
 */
const ROW_BADGE: Record<RowState, string> = {
  ok: "",
  broken: "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400",
  disabled: "border-border bg-muted text-muted-foreground",
  pending: "border-amber-500/45 bg-amber-500/10 text-amber-700 dark:text-amber-400",
}

/** 行的外壳（底 + 描边）。缺依赖与待重启必须**整行**看得出不对劲，不能只靠一个小徽章。 */
const ROW_SHELL: Record<RowState, string> = {
  ok: "border-border bg-background/60",
  broken: "border-red-500/35 bg-red-500/5",
  disabled: "border-border bg-muted/40",
  pending: "border-amber-500/40 bg-amber-500/5",
}

/**
 * 左侧 3px 状态条 —— 第二重编码。
 * 徽章给「看得懂字的」，状态条给「一眼扫过去的」：缩略图尺度下颜色会糊，
 * 但「左边有没有一条」这个形状永远分得出。
 */
const ROW_BAR: Record<RowState, string> = {
  ok: "",
  broken: "bg-red-500",
  disabled: "bg-muted-foreground/40",
  pending: "bg-amber-500",
}

/** 状态条与徽章的字色（amber 系复用同一对，避免两处各写一遍主题分支）。 */
const CHIP_TONE: Record<"muted" | "danger" | "warn", string> = {
  muted: "bg-muted text-muted-foreground",
  danger: "bg-red-500/10 text-red-600 dark:text-red-400",
  warn: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
}

/** 一个能力要占多少硬盘 —— `extras` 里声明的体积（模型 + 要自备的东西）。 */
function sizeMb(p: PluginEntry): number {
  const items = [...(p.extras?.models ?? []), ...(p.extras?.external ?? [])]
  return items.reduce((n, x) => n + (x.size_hint_mb ?? 0), 0)
}

function formatMb(mb: number): string {
  if (!mb) return ""
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`
}

function hasExtras(p: PluginEntry): boolean {
  return Boolean(p.extras?.python?.length || p.extras?.external?.length || p.extras?.models?.length)
}

/**
 * 后端三态 + 前端 pending → 行状态。
 *
 * **`broken` 永远优先**：一个坏掉的能力如果本次恰好也被改过，把红换成琥珀等于
 * 用「待重启」盖住「它根本没加载起来」。坏掉是第一信息，别的都往后排。
 */
function rowStateOf(p: PluginEntry, pending: boolean): RowState {
  if (p.state === "broken") return "broken"
  if (pending) return "pending"
  return p.state
}

/** 只有例外挂徽章。「可用」返回 `null`，不留空位、不占宽度。 */
function StateBadge({ state }: { state: RowState }) {
  if (state === "ok") return null
  return (
    <span
      className={cn(
        "shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium",
        ROW_BADGE[state],
      )}
    >
      {ROW_LABEL[state]}
    </span>
  )
}

/** 卡面上的一行小字：状态缺口 / 代价 / 锁定原因。统一形状，扫读时不用重新认。 */
function Chip({
  tone = "muted",
  title,
  children,
}: {
  tone?: "muted" | "danger" | "warn"
  title?: string
  children: React.ReactNode
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex max-w-full items-center truncate rounded-md px-1.5 py-0.5 font-mono text-[11px]",
        CHIP_TONE[tone],
      )}
    >
      {children}
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
        // 「在开」用品牌紫，不用绿：绿是 success 的意思，而这里只是一次状态切换，
        // 满屏绿开关还会和「可用」的绿徽章（已删）抢同一套语义。
        on ? "bg-primary" : "bg-muted-foreground/30",
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
    return <p className="text-[11px] leading-5 text-muted-foreground">探针没跑起来：{probe.error}</p>
  }
  const parts = Object.entries(probe.data ?? {})
    // 空值不摊：否则每行都挂着一串 `rva=null · dll=null · reason=`，噪声盖过信号
    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0))
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
  if (!parts.length) return null
  return (
    <p className="font-mono text-[11px] leading-5 text-muted-foreground">
      实时状态：{parts.join(" · ")}
    </p>
  )
}

/** 一个能力要装什么 —— 三项都空就不占版面。第 7 步起只活在「详情」里。 */
function Extras({ p }: { p: PluginEntry }) {
  const py = p.extras?.python ?? []
  const external = p.extras?.external ?? []
  const models = p.extras?.models ?? []
  if (!py.length && !external.length && !models.length) return null
  return (
    <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] leading-5 text-muted-foreground">
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
  pending,
  onToggle,
  onCloseWith,
}: {
  p: PluginEntry
  busy?: boolean
  /** 本次会话改过 → 待重启 */
  pending?: boolean
  onToggle?: (id: string, on: boolean) => void
  /** 「一并关闭这 N 项」—— 依赖守卫的可执行出口 */
  onCloseWith?: (id: string, name: string, blockers: string[]) => void
}) {
  /** 元信息默认折叠。每个能力各记各的 —— 展开一个不该把别的也撑开。 */
  const [showDetail, setShowDetail] = useState(false)

  const on = p.enabled ?? p.state !== "disabled"
  const locked = p.core
  const blockers = p.blockedBy ?? []
  const state = rowStateOf(p, Boolean(pending))
  const saved = formatMb(sizeMb(p))
  /** 缺口只取首条 —— 完整列表在详情里。多的原因是"能跑起来之前先解决第一个"。 */
  const gap = p.reasons[0] ?? ""
  const hasDetail = Boolean(
    p.reasons.length || p.requires.length || p.disableNote || p.healthProbe || hasExtras(p),
  )

  return (
    <li className={cn("relative overflow-hidden rounded-lg border px-3 py-2.5", ROW_SHELL[state])}>
      {ROW_BAR[state] ? (
        <span
          className={cn("absolute bottom-2.5 left-0 top-2.5 w-[3px] rounded-r", ROW_BAR[state])}
          aria-hidden="true"
        />
      ) : null}

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-card-foreground">
            {p.name}
            <code className="ml-2 font-mono text-[11px] font-normal text-muted-foreground">{p.id}</code>
          </p>
          <p className="mt-0.5 text-xs leading-5 text-muted-foreground">{p.summary}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StateBadge state={state} />
          {onToggle ? (
            <Switch
              on={on}
              busy={busy}
              disabled={locked}
              label={`${locked ? "核心能力，不可关闭" : on ? "关闭" : "开启"} ${p.name}`}
              title={
                locked
                  ? "核心能力，不能关闭（关掉它整个界面就没有意义了）"
                  : blockers.length && on
                    ? `仍被 ${blockers.join("、")} 依赖；要先关掉它们`
                    : on
                      ? "关闭这个能力（重启后生效）"
                      : "开启这个能力（重启后生效）"
              }
              onToggle={() => onToggle(p.id, !on)}
            />
          ) : null}
        </div>
      </div>

      {/* 卡面小字：先说「为什么不能用」，再说「关掉能省多少」。
          以前这里只有一串元信息，用户看完仍然不知道这两件事。 */}
      {gap || (locked && p.disableNote) || saved ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {gap ? (
            <Chip tone="danger" title={p.reasons.join(" · ")}>
              {gap}
            </Chip>
          ) : null}
          {locked && p.disableNote ? <Chip>{p.disableNote}</Chip> : null}
          {saved ? (
            <Chip tone={state === "pending" ? "warn" : "muted"}>
              {on ? `关掉可省 ≈${saved}` : `已省 ≈${saved}`}
            </Chip>
          ) : null}
        </div>
      ) : null}

      {/* 守卫：把一个约束说成**可执行的动作**。
          以前只有一行灰字「被依赖：xxx（想关它，先关掉这些）」—— 用户得自己回去找那几个开关。
          detail 仍然由后端的 409 决定（前端不重算依赖），这里只是把执行路径铺好。 */}
      {blockers.length ? (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <p className="text-[11px] leading-5 text-muted-foreground">
            被依赖：{blockers.join("、")} —— 想关它，先关掉这些
          </p>
          {onCloseWith && on ? (
            <button
              type="button"
              onClick={() => onCloseWith(p.id, p.name, blockers)}
              disabled={busy}
              title={`一并关闭：${[...blockers, p.id].join("、")}`}
              className="rounded-md border border-red-500/40 bg-red-500/10 px-2 py-0.5 text-[11px] font-medium text-red-600 transition hover:bg-red-500/15 disabled:opacity-60 dark:text-red-400"
            >
              一并关闭这 {blockers.length + 1} 项
            </button>
          ) : null}
        </div>
      ) : null}

      {hasDetail ? (
        <>
          <button
            type="button"
            aria-expanded={showDetail}
            onClick={() => setShowDetail((v) => !v)}
            className="mt-1.5 flex items-center gap-0.5 text-[11px] font-medium text-muted-foreground transition hover:text-foreground"
          >
            <ChevronRight
              className={cn("h-3 w-3 transition-transform", showDetail && "rotate-90")}
              aria-hidden="true"
            />
            详情
          </button>
          {/* 字段**一条都没丢**，只是默认不出现 —— 排查时展开，日常不打扰。 */}
          {showDetail ? (
            <div className="mt-1.5 space-y-1 rounded-lg border border-border bg-muted/30 px-2.5 py-2">
              {p.reasons.length ? (
                <ul className="space-y-0.5">
                  {p.reasons.map((r) => (
                    <li key={r} className="font-mono text-[11px] leading-5 text-red-600 dark:text-red-400">
                      {r}
                    </li>
                  ))}
                </ul>
              ) : null}
              <Extras p={p} />
              <HealthProbe probe={p.healthProbe ?? null} />
              {p.disableNote ? (
                <p className="text-[11px] leading-5 text-muted-foreground/80">{p.disableNote}</p>
              ) : null}
              {p.requires.length ? (
                <p className="text-[11px] leading-5 text-muted-foreground">
                  依赖：{p.requires.join("、")}
                </p>
              ) : null}
            </div>
          ) : null}
        </>
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
          tone === "danger"
            ? "text-red-600 dark:text-red-400"
            : tone === "warn"
              ? "text-amber-700 dark:text-amber-400"
              : "text-foreground",
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
  /** 忙碌的能力 id，或 `"preset:<name>"` / `"restart"` */
  const [busy, setBusy] = useState<string | null>(null)
  /** 开关被拒 / 写成功后的提示。写成功也要提示 —— 否则"点了没变化"会被当成坏了。 */
  const [notice, setNotice] = useState<{ tone: "ok" | "warn" | "error"; text: string } | null>(null)
  /**
   * 本次会话里**改过但还没重启**的能力。
   *
   * 以前这里是一个 `boolean dirty`，只能整体说一句「改动已保存，要重启才生效」，
   * 具体是哪几项得用户自己回忆。改成按 id 记，才能做到两件事：
   * ① 顶部说清「N 项待重启」；② 在那几行上直接挂琥珀徽章（改完当场有反馈）。
   */
  const [pendingIds, setPendingIds] = useState<Set<string>>(() => new Set())

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

  const plugins = useMemo(() => data?.plugins ?? [], [data])

  const addPending = useCallback((ids: string[]) => {
    if (!ids.length) return
    setPendingIds((prev) => new Set([...prev, ...ids]))
  }, [])

  const toggle = useCallback(
    async (id: string, on: boolean) => {
      setBusy(id)
      setNotice(null)
      try {
        await setPluginEnabled(id, on)
        addPending([id])
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
    [addPending, data, load],
  )

  /**
   * 「一并关闭这 N 项」。
   *
   * 顺序**不能反**：先关依赖者、再关它自己，否则后端一定用 409 把我们拦在第一步。
   * 中途失败时把**已经关掉的那几项如实报出来** —— 一次点了 3 项、成了 2 项却只说
   * 「失败了」，用户会以为一项都没动。
   */
  const closeWithDependents = useCallback(
    async (id: string, name: string, blockers: string[]) => {
      setBusy(id)
      setNotice(null)
      const done: string[] = []
      try {
        for (const dep of blockers) {
          await setPluginEnabled(dep, false)
          done.push(dep)
        }
        await setPluginEnabled(id, false)
        addPending([...done, id])
        setNotice({
          tone: "ok",
          text: `已一并关闭「${name}」等 ${done.length + 1} 项（重启后生效）。`,
        })
      } catch (e) {
        addPending(done)
        setNotice({
          tone: "error",
          text: `${done.length ? `已关闭 ${done.join("、")}；` : ""}${e instanceof Error ? e.message : String(e)}`,
        })
      } finally {
        await load()
        setBusy(null)
      }
    },
    [addPending, load],
  )

  const applyPreset = useCallback(
    async (name: string, label: string) => {
      setBusy(`preset:${name}`)
      setNotice(null)
      try {
        await applyPluginPreset(name)
        // 套餐动的是**整组**，所以整组都算待重启 —— 只记名字会漏掉被它连带改动的项
        addPending(plugins.map((p) => p.id))
        setNotice({ tone: "ok", text: `已套用「${label}」套餐，重启应用后生效。` })
        await load()
      } catch (e) {
        setNotice({ tone: "error", text: e instanceof Error ? e.message : String(e) })
      } finally {
        setBusy(null)
      }
    },
    [addPending, load, plugins],
  )

  /**
   * 「立即重启」= 重启后端进程 + 刷新界面。
   *
   * 插件路由是后端**启动时**挂的，所以真正要重来的是后端，不是这个窗口 ——
   * 让用户「退出应用再打开」是把这个知识推给他。非桌面端（纯网页调试）没有这个通道，
   * 降级成手动提示，不假装成功。
   */
  const restart = useCallback(async () => {
    setBusy("restart")
    setNotice(null)
    try {
      const r = await restartBackend()
      if (!r) {
        setNotice({ tone: "warn", text: "当前不是桌面端，无法自动重启 —— 请手动重启后端后刷新页面。" })
        return
      }
      if (!r.running) {
        setNotice({ tone: "error", text: `重启失败：${r.reason ?? "未知错误"}` })
        return
      }
      setPendingIds(new Set())
      window.location.reload()
    } catch (e) {
      setNotice({ tone: "error", text: `重启失败：${e instanceof Error ? e.message : String(e)}` })
    } finally {
      setBusy(null)
    }
  }, [])

  if (!open) return null

  const counts = data?.counts
  const presets = data?.presets ?? []
  const disabledList = plugins.filter((p) => p.state === "disabled")
  const broken = plugins.filter((p) => p.state === "broken")
  const usable = plugins.filter((p) => p.state === "ok")
  const groups = CATEGORY_ORDER.map((c) => [c, usable.filter((p) => p.category === c)] as const).filter(
    ([, list]) => list.length > 0,
  )
  const pendingCount = pendingIds.size
  /** 待重启的项里**已经关掉**的那部分省下来的体积 —— 开着的还没省下什么。 */
  const pendingSaved = formatMb(
    plugins.reduce((n, p) => {
      if (!pendingIds.has(p.id)) return n
      const on = p.enabled ?? p.state !== "disabled"
      return on ? n : n + sizeMb(p)
    }, 0),
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
                  {pendingCount ? ` · ${pendingCount} 待重启` : ""}
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

        {/* 待重启条：**常驻在顶部**，不是插在内容中间的一段灰字。
            它就是这一屏最容易漏掉的那一态 —— 点了开关界面毫无变化，用户只会以为开关坏了。 */}
        {pendingCount ? (
          <div
            role="status"
            className="mt-4 flex items-center gap-2 rounded-xl border border-amber-500/40 bg-amber-500/5 px-3 py-2"
          >
            <RotateCcw className="h-3.5 w-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
            <p className="flex-1 text-xs leading-5 text-amber-700 dark:text-amber-400">
              {pendingCount} 项能力有改动待重启 —— 关掉的不会加载，也就不占显存
              {pendingSaved ? `，共省 ≈${pendingSaved}` : ""}。
            </p>
            <button
              type="button"
              onClick={() => void restart()}
              disabled={busy === "restart"}
              title="重启后端服务并刷新界面，不用退出应用"
              className="flex shrink-0 items-center gap-1 rounded-md border border-amber-500/50 px-2 py-0.5 text-[11px] font-medium text-amber-700 transition hover:bg-amber-500/10 disabled:opacity-60 dark:text-amber-400"
            >
              {busy === "restart" ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
              立即重启
            </button>
          </div>
        ) : null}

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
                ? "border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400"
                : notice.tone === "warn"
                  ? "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
                  : "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
            )}
          >
            {notice.text}
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
                      // 当前套餐不让"选中的绿" —— 绿留给成功提示，这里用品牌色
                      active
                        ? "border-primary/50 bg-primary/10 text-primary"
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
            title={`已关闭的能力（${disabledList.length}）`}
            hint="关掉的能力不会加载 —— 不占显存、不注册路由、界面上也不出现。这一区存在的意义只有一个：让「功能去哪了」有个答案。"
            icon={<PowerOff className="h-3.5 w-3.5" />}
          >
            {disabledList.length ? (
              <ul className="space-y-2">
                {disabledList.map((p) => (
                  <CapabilityRow
                    key={p.id}
                    p={p}
                    busy={busy === p.id}
                    pending={pendingIds.has(p.id)}
                    onToggle={toggle}
                    onCloseWith={closeWithDependents}
                  />
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
                  <CapabilityRow
                    key={p.id}
                    p={p}
                    busy={busy === p.id}
                    pending={pendingIds.has(p.id)}
                    onToggle={toggle}
                    onCloseWith={closeWithDependents}
                  />
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
                      <CapabilityRow
                        key={p.id}
                        p={p}
                        busy={busy === p.id}
                        pending={pendingIds.has(p.id)}
                        onToggle={toggle}
                        onCloseWith={closeWithDependents}
                      />
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
