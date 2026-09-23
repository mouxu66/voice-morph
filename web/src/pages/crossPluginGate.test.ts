import { existsSync, readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

/**
 * 「宿主页托管**别的**插件」这一类缺口的门控门禁（2026-09-23）。
 *
 * 背景 —— 这是一整类 bug，不是五处孤立的小毛病：
 * 一个页面属某个插件（它的路由随该插件消失），但页内的 tab / 面板可能打的是
 * **另一个可关插件**的端点。此时「宿主开着、那个能力关着」完全可达，页内那块
 * 却照旧渲染 → 用户点一下 404，且完全看不出原因。
 *
 * 为什么以前没人发现：`tools/audit_endpoint_ownership.py` 的旧判据一进来就
 * `if not is_core.get(pid): continue`（"非核心路由整条会随插件消失"），
 * 把**所有非核心路由页**整条豁免了 —— 而它漏掉的正是上面这个状态。
 * 判据已在该脚本里改成「按 requires 闭包判断是否注定同生共死」，
 * 这份文件负责钉住**前端側真的接上了门控**（判据只能说"没人点过这个 id"）。
 *
 * ★ **两种修法，别搞混**（2026-09-23 同一天里两种都用了）：
 *
 * 1. **页内门控**（Workshop / OfflineVc / Audition / Tts 卡片）：耦合是真的，
 *    所以把那一块按 `pluginVisible` 显隐。
 * 2. **改清单归属，从根上消掉耦合**（Live 页）：`rvc_dataset_api`（四个数据集端点
 *    加 `/rvc/model`）服务的是 Live 页驱动的 RVC 训练流程，却曾经被写在 `sound.ft`
 *    名下 → 凭空造出「关掉音色微调→实时变声页也瘸」。它跟 `finetune.py`（`/ft/*`，
 *    用录音继续训练 **TTS** 音色）是两件事，划回 `sound.rvc-live` 之后 Live 页
 *    一个跨插件调用都不剩，**反而不该再门控**。
 *
 *    ⇒ 所以下面既有「必须门控」的断言，也有「**不许**门控」的断言。
 *    后者同样重要：过度门控 = 把还能用的功能从界面上藏掉。
 *
 * 为什么是源码断言而不是渲染测试：这一整类缺口分布在 9 个文件里、各自要造
 * 不同的后端桩；而真正要防的回归是"有人把条件渲染改回无条件"。与
 * `pages/Tts/index.gate.test.ts`、`pages/Voices/index.gate.test.ts` 同一手法。
 *
 * 关于「插件 id 拼错」：这里**没有**单独加一条拼写守卫 —— 因为 `pluginVisible`
 * 对清单里不存在的 id 返回 `true`（旧后端兼容策略），拼错会让门控静默失效，
 * 但 `audit_endpoint_ownership` 会把拼错的那个 id 当成"链上没人点过"从而报缺口，
 * 即拼写错误已经由那道门禁兜住了。别在这里重复维护一份 id 表。
 */

/** 统一成 LF：仓库里部分 .tsx 是 CRLF，多行断言不做归一化会莫名其妙地红。 */
function readRel(rel: string): string {
  return readFileSync(path.join(process.cwd(), "src", rel), "utf8").replace(/\r\n/g, "\n")
}

/** 找 `m2_server/plugins`（与 `lib/pluginRoutes.test.ts` 同一套：找不到就抛，绝不静默返回空目录）。 */
function findPluginsDir(): string {
  let dir = process.cwd()
  for (let i = 0; i < 4; i += 1) {
    const cand = path.resolve(dir, "m2_server", "plugins")
    if (existsSync(cand)) return cand
    dir = path.resolve(dir, "..")
  }
  throw new Error(`找不到 m2_server/plugins（cwd=${process.cwd()}）`)
}

/** 读**真实**清单（不手写 fixture —— 手写只会照抄我自己的假设）。 */
function readManifest(id: string): { routers: string[] } {
  const p = path.join(findPluginsDir(), id, "plugin.json")
  return JSON.parse(readFileSync(p, "utf8")) as { routers: string[] }
}

// ---------------------------------------------------------------- 音色工坊

describe("Workshop 的「微调」「发掘」tab 门控", () => {
  const src = readRel(path.join("pages", "Workshop", "index.tsx"))

  it("两个 tab 的开关都来自 pluginVisible，且用的是真实 id", () => {
    expect(src).toContain('pluginVisible(catalog, "sound.mine")')
    expect(src).toContain('pluginVisible(catalog, "sound.ft")')
  })

  it("★ 微调 tab 按 sound.ft 条件渲染（宿主是 sound.workshop，不是同一个插件）", () => {
    // 关键事实：sound.ft.requires 含 sound.workshop（依赖方向是反的），
    // 所以「工坊开着、微调关着」可达 —— 宿主的开关替不了这个 tab。
    expect(src).toContain("...(ftOn ? [{ key: \"ft\"")
    expect(src).not.toContain('{ key: "ft", label: "微调", content: <FtPage {...useFt()} /> }')
  })

  it("发掘 tab 按 sound.mine 条件渲染", () => {
    expect(src).toContain("...(discoverOn ? [{ key: \"discover\"")
    // ⚠️ 这里**不能**用 `not.toContain('{ key: "discover", ...<DiscoverPage /> }')` 做反向断言：
    // DiscoverPage 不接 props，那个子串在条件渲染里本来就存在（2026-09-23 实测踩到）。
    // 「真的接上了门控」由上面那条正向断言 + 后面的 Python 缺口判据共同保证。
  })

  it("hook 必须无条件调用（React 规则），显隐只发生在 tabs 数组里", () => {
    expect(src).toContain("const workshop = useWorkshop()")
    expect(src).toContain("const ft = useFt(ftOn)")
  })
})

describe("useFt 的 enabled 契约", () => {
  const src = readRel(path.join("pages", "Ft", "useFt.ts"))

  it("签名收 enabled，默认 true（旧调用点不传也照旧）", () => {
    expect(src).toContain("export function useFt(enabled = true) {")
  })

  it("★ 关掉时不轮询（端点已卸载，轮询只是空转打 404）", () => {
    expect(src).toContain("if (!enabled || !voiceId) return;")
    expect(src).toContain("}, [enabled, voiceId, runQc]);")
  })
})

// ---------------------------------------------------------------- 离线工坊

describe("OfflineVc 的「效果器」tab 门控", () => {
  const routeSrc = readRel(path.join("pages", "OfflineVc", "index.tsx"))
  const hookSrc = readRel(path.join("pages", "Effects", "useEffects.ts"))

  it("效果器 tab 按 sound.effects 条件渲染（宿主是 sound.offline-vc）", () => {
    expect(routeSrc).toContain('pluginVisible(catalog, "sound.effects")')
    expect(routeSrc).toContain("...(fxOn ? [{ key: \"fx\"")
    expect(routeSrc).not.toContain('{ key: "fx", label: "效果器", content: <EffectsPage {...useEffects()} /> }')
  })

  it("前两个 tab 属本页自己的插件，不该被门控（别过度门控把功能藏了）", () => {
    expect(routeSrc).not.toContain('pluginVisible(catalog, "sound.offline-vc")')
    expect(routeSrc).toContain('<OfflineVcPage {...ovc} />')
    expect(routeSrc).toContain('<SeedVcPage {...seed} />')
  })

  it("★ useEffects 的目录加载在 enabled=false 时不发请求（它挂载即 fetch）", () => {
    expect(hookSrc).toContain("export function useEffects(enabled = true) {")
    expect(hookSrc).toContain("if (!enabled) return")
    expect(hookSrc).toContain("}, [enabled])")
  })
})

// ------------------------------------------------ 实时变声：归属已从根上修好

describe("Live 流水线的数据面归属（从 sound.ft 划回 sound.rvc-live）", () => {
  const hookSrc = readRel(path.join("pages", "Live", "useLive.ts"))
  const pageSrc = readRel(path.join("pages", "Live", "LivePage.tsx"))

  it("★ rvc_dataset_api 归 sound.rvc-live；finetune 归 sound.ft —— 两件事不该混在一个插件里", () => {
    // 这是 2026-09-23 那次「从根上修」：四个数据集端点（生成语料/导入/状态/列表）
    // 加 `/rvc/model` 全在 `rvc_dataset_api.py` 里，服务的是 **Live 页驱动的 RVC 训练流程**；
    // 而 `finetune.py`（`/ft/*`）是用录音继续训练 **TTS** 音色（惰性 import qwen3_tts）。
    // 之前两者都被写在 `sound.ft` 名下，凭空造出「关掉音色微调 → 实时变声页也瘸」的耦合。
    expect(readManifest("sound.rvc-live").routers).toContain("rvc_dataset_api")
    expect(readManifest("sound.ft").routers).toEqual(["finetune"])
  })

  it("★ Live 页不许再按 sound.ft 门控（那会藏掉本来还能用的两步）", () => {
    // 归属改对之后，这两步打的是**本页自己插件**的端点 —— 只要本页在，它们就在。
    // 再按 sound.ft 门控就是“过度门控”：把可用功能从界面上藏掉。
    expect(hookSrc).not.toContain("ftOn")
    expect(pageSrc).not.toContain("p.ftOn")
    // 两步本身必须还在
    expect(pageSrc).toContain('label="生成语料"')
    expect(pageSrc).toContain('label="导入 RVC"')
  })
})

// ---------------------------------------------------------------- 试音间

describe("Audition 的「一个个实时试」档", () => {
  const hookSrc = readRel(path.join("pages", "Audition", "useAudition.ts"))
  const pageSrc = readRel(path.join("pages", "Audition", "AuditionPage.tsx"))

  it("开关按 sound.rvc-live（本页属 sound.audition，不 requires 它）", () => {
    expect(hookSrc).toContain('pluginVisible(catalog, "sound.rvc-live")')
    expect(hookSrc).toContain("const liveOn = pluginVisible")
  })

  it("★ refreshLive 在关掉时直接返回（它在首屏与任务轮询里被多处调用）", () => {
    expect(hookSrc).toContain("if (!liveOn) return")
    expect(hookSrc).toContain("}, [liveOn])")
  })

  it("★ 派生一个 witMode，而不是逐处改判断（漏一处就漏一处 404）", () => {
    // 页面里 `witMode === "live"` 有十来处（按钮高亮、点选行为、文案）。
    // 逐处加 `&& liveOn` 极易漏；派生的写法让它们**全部**自动拿到正确结果。
    expect(pageSrc).toContain('const witMode = p.liveOn ? witModeState : "batch"')
    expect(pageSrc).toContain('.filter(([key]) => key !== "live" || p.liveOn)')
  })
})

// ---------------------------------------------------------------- TTS 页的卡片

describe("Tts 页的 RVC 模型卡（打的是 sound.rvc-live 的端点）", () => {
  const src = readRel(path.join("pages", "Tts", "RvcDatasetCard.tsx"))

  it("★ 卡片自己门控 —— 否则关掉实时变声后会一路显示「检测失败」，误导用户去重训", () => {
    // 门控的 id 必须是 sound.rvc-live（`/rvc/model` 属 rvc_dataset_api），
    // **不是** sound.ft —— 这卡描述的是实时变声模型，跟「用录音训练 TTS 音色」无关。
    expect(src).toContain('pluginVisible(catalog, "sound.rvc-live")')
    expect(src).not.toContain('pluginVisible(catalog, "sound.ft")')
    expect(src).toContain("if (!liveOn) return\n")
    expect(src).toContain("}, [liveOn])")
  })

  it("早退发生在所有 hook 之后（React 规则）", () => {
    const guard = src.indexOf("if (!liveOn) return null")
    const lastHook = Math.max(src.lastIndexOf("useState("), src.lastIndexOf("useEffect("))
    expect(guard).toBeGreaterThan(-1)
    expect(guard).toBeGreaterThan(lastHook)
  })
})
