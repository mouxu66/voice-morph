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

// ---------------------------------------------------------------- 特效声板

describe("特效声板的 `sound.fx-board` 门控", () => {
  const panelSrc = readRel(path.join("pages", "Tts", "SoundboardPanel.tsx"))
  const hookSrc = readRel(path.join("pages", "Tts", "useSoundboard.ts"))
  const pageSrc = readRel(path.join("pages", "Tts", "WechatSendPage.tsx"))

  it("清单里真有这个插件，且只认领 soundboard 一个 router", () => {
    expect(readManifest("sound.fx-board").routers).toEqual(["soundboard"])
  })

  it("★ 面板**自己**门控 —— 宿主页属 hook.wechat，不是 sound.fx-board", () => {
    // 门控写在组件里而不是调用方：无论谁把面板塞进哪个页面都安全。
    expect(panelSrc).toContain('pluginVisible(catalog, "sound.fx-board")')
    expect(panelSrc).toContain("if (!on) return null")
  })

  it("hook 的 enabled 契约：关掉时既不预热也不拉目录（端点已卸载）", () => {
    expect(hookSrc).toContain("export function useSoundboard(enabled = true) {")
    expect(hookSrc).toContain("if (!enabled || !backendUp) return")
  })

  it("★ 拉目录不串在预热之后：预热握手可能长时间不返回，面板不该陪着空等", () => {
    // `/soundboard/catalog` 是毫秒级只读接口（真机实测 25ms），而预热要拉起常驻播放器、
    // 走一次**没有超时**的就绪握手（`m2_server/soundboard.py::_spawn_worker` 的 `readline()`）。
    // 串行 `await soundboardWarm()` 之后再取数时，握手慢/卡住就等于把目录请求永远挂在后面，
    // 面板停成"共 0 条（出厂 0）"——2026-09-25 真机踩到，用户直接以为这功能不存在。
    expect(hookSrc).not.toContain("await soundboardWarm()")
    expect(hookSrc).toContain("void soundboardWarm().catch(() => {})")
  })

  it("★ 路由层把「宿主开着」与「自己开着」相与后才启用（hook 只调一次）", () => {
    const routeSrc = readRel(path.join("pages", "Tts", "index.tsx"))
    expect(routeSrc).toContain('pluginVisible(catalog, "sound.fx-board")')
    expect(routeSrc).toContain("useSoundboard(wechatOn && fxBoardOn)")
  })

  it("挂在 ② 半自动 与 ③ 手动 两处（半自动是用户主场景之二）", () => {
    expect(pageSrc.match(/<SoundboardPanel/g)?.length).toBe(2)
    // 两处都吃路由层那一个 hook 实例：各调一次就会两回预热 + 两回目录请求
    expect(pageSrc).toContain("<SoundboardPanel sb={p.soundboard}")
  })

  it("★ 声板不抢发送锁：不许因 busy 禁用它 —— 那正是要出声的时候", () => {
    // ② 半自动在播放 TTS 时 `busy === "play"`；后端那边声板刻意不进 `_send_lock`
    // （见 `m2_server/soundboard.py` 模块头与 `test_soundboard.py` 的不变量用例）。
    // 前端如果给格子加上 `|| busy`，就等于把「录制中点一下」这个主场景禁掉了，
    // 而且症状很隐：按钮变灰，用户只会以为坏了。
    expect(panelSrc).not.toMatch(/disabled=\{[^}]*busy/)
  })
})

// ---------------------------------------------------------------- 声板素材（导入 / 音效包）

describe("声板素材管理（单条导入 + 成套音效包）", () => {
  const panelSrc = readRel(path.join("pages", "Tts", "SoundboardPanel.tsx"))
  const hookSrc = readRel(path.join("pages", "Tts", "useSoundboard.ts"))
  const clientSrc = readRel(path.join("api", "client.ts"))

  it("★ 四个动作都打声板自己的端点（不绕道别的可关插件）", () => {
    for (const call of [
      'jsonFetch("/soundboard/import"',
      'jsonFetch("/soundboard/packs"',
      'jsonFetch("/soundboard/packs/install"',
      'jsonFetch("/soundboard/packs/download"',
    ]) {
      expect(clientSrc).toContain(call)
    }
    expect(panelSrc).not.toContain("/effects/")
    expect(hookSrc).not.toContain("useEffects")
  })

  it("★ 素材动作彼此互斥（否则目录刷新乱序，界面会停在「少一条」的状态）", () => {
    // 两个动作同时在飞时，后完成的那次刷新可能先落库 —— 而那是一个"刷新一次就对了"
    // 的偶发错，最难查。互斥写在 hook 的公共壳里（不在各个按钮上），所以钉它。
    expect(hookSrc).toContain("if (busy) return null")
    expect(panelSrc).toContain("const materialLocked =")
    // 素材锁与格子锁必须是两个名字：格子**绝不能**因 busy 变灰（发送中正是要出声时），
    // 上一条 describe 里的 `disabled={…busy}` 守卫靠这个区分才能继续有效。
    expect(panelSrc).toContain("disabled={materialLocked}")
  })

  it("三个入口都在：导入素材 / 安装音效包 / 音效包市场", () => {
    expect(panelSrc).toContain("导入素材")
    expect(panelSrc).toContain("安装音效包")
    expect(panelSrc).toContain("音效包市场")
  })

  it("★ 许可与作者必须看得见（包会被装到别人机器上，界面是那边唯一的依据）", () => {
    expect(panelSrc).toContain("p.license")
    expect(panelSrc).toContain("未标注许可")
    expect(panelSrc).toContain("p.author")
  })

  it("★ 坏包如实列出来（带原因），不是当它不存在", () => {
    expect(panelSrc).toContain("p.broken")
  })

  it("★ 「能不能单条删」由后端说：前端不许自己推 builtin/pack", () => {
    expect(panelSrc).toContain("i.removable")
  })

  it("★ 货架懒加载：没点开之前不请求（不白碰一次网络）", () => {
    expect(hookSrc).toContain("if (shelf.loaded) return")
    expect(hookSrc).toContain("soundboardPacksAvailable")
  })
})

// ---------------------------------------------------------------- 声板预混模式

describe("声板预混模式（发送前把音效烘进音频）", () => {
  const panelSrc = readRel(path.join("pages", "Tts", "SoundboardPanel.tsx"))
  const hookSrc = readRel(path.join("pages", "Tts", "useSoundboard.ts"))
  const sendHookSrc = readRel(path.join("pages", "Tts", "useWechatSend.ts"))
  const pageSrc = readRel(path.join("pages", "Tts", "WechatSendPage.tsx"))
  const clientSrc = readRel(path.join("api", "client.ts"))

  it("★ 预混打的是声板自己的端点（`/soundboard/premix`），不绕道效果器", () => {
    // 绕道 `/effects/apply` 会同时坏两件事：① 关掉 `sound.effects` 后按钮 404；
    // ② 多一步"把 outputs 里的文件读出来再上传"的搬运。
    expect(clientSrc).toContain('jsonFetch("/soundboard/premix"')
    expect(panelSrc).not.toContain("/effects/")
    expect(hookSrc).not.toContain("/effects/")
  })

  it("★ 预混不依赖 sound.effects 开着（后端也不许 import effects）", () => {
    // 与 `m2_server/tests/test_premix.py::test_premix_does_not_need_the_effects_plugin`
    // 是一对：那边守后端模块依赖，这里守前端不加多余门控。
    expect(panelSrc).not.toContain("sound.effects")
    expect(hookSrc).not.toContain("useEffects")
  })

  it("预混只在 ② 半自动提供（③ 手动档没有合成产物可混）", () => {
    expect(pageSrc.match(/allowPremix/g)?.length).toBe(1)
    expect(pageSrc).toContain("wav={p.lastTts?.wav}")
  })

  it("★ 发送目标是预混产物；不许把 undefined 裸透给后端", () => {
    // 后端把"没给 wav"理解成「取最近一条 `tts_*.wav`」—— 裸透 undefined 会**绕过**预混结果，
    // 于是用户看到"已混入音效"的徽标、发出去的却是没混的那条（且完全无报错）。
    expect(sendHookSrc).toContain("wechatSendVoice(wav ?? targetWav)")
    expect(sendHookSrc).toContain("wechatPlayToCable(wav ?? targetWav)")
    expect(pageSrc).toContain("void p.sendAuto()")
    expect(pageSrc).toContain("void p.playToCable()")
  })

  it("★ 合成产物一变，预混自动失效（否则会发出上一次混过的旧内容）", () => {
    expect(sendHookSrc).toContain("premix.source === sourceWav")
    expect(sendHookSrc).toContain("const targetWav = premixActive && premix ? premix.wav : sourceWav")
  })

  it("失败的预混要显示出来（用户必须看到「那条音效没混进去」）", () => {
    expect(hookSrc).toContain("setPremixError(friendlyError(error, \"预混失败\"))")
    expect(panelSrc).toContain("sb.premixError")
  })

  it("★ 一步到位：混好即自动发送（不是「② 混一次 → ① 再按一次」）", () => {
    // 两步之间那个「发送目标已换成 sfxmix_*」是界面上**看不见**的中间态：用户在第 ② 步
    // 之后去别处（重新合成一条）再点 ① ，发出去的就是没混的那条，且全程无报错。
    // 所以按钮把 wav **显式**交给发送方，不依赖 `targetWav` 的失效判定。
    expect(panelSrc).toContain("onSendPremixed")
    expect(panelSrc).toContain("混好并直接发送")
    expect(pageSrc).toContain("onSendPremixed={(wav) => void p.sendAuto(wav)}")
    // 只有 ② 有它：③ 手动档与实时页都没有"要发送的那条产物"这一步。
    expect(readRel(path.join("pages", "Live", "LivePage.tsx"))).not.toContain("onSendPremixed")
  })
})

// ------------------------------------------------ 预混的精确时间点

describe("预混可以指定「第 N 秒」（不是只能开头/叠加/结尾）", () => {
  const panelSrc = readRel(path.join("pages", "Tts", "SoundboardPanel.tsx"))
  const hookSrc = readRel(path.join("pages", "Tts", "useSoundboard.ts"))
  const pageSrc = readRel(path.join("pages", "Tts", "WechatSendPage.tsx"))

  it("★ `at_s` 只跟「叠加」档一起发", () => {
    // 开头/结尾是拼接，位置由 mode 本身决定；带着没意义的 0 一起发，
    // 读日志的人会以为它生效了（后端也确实只在 layer 分支里读它）。
    expect(hookSrc).toContain('at_s: p.mode === "layer" ? p.at_s : 0')
    expect(hookSrc).toContain("setPickAt")
  })

  it("★ 秒数输入只在叠加档上出现（其余位置里它是死参数）", () => {
    expect(panelSrc).toContain('p.mode === "layer" && (')
    expect(panelSrc).toContain("sb.setPickAt(p.sample, Number(e.target.value))")
  })

  it("★ 越界不许静默：后端那句回报必须显示出来", () => {
    // 用户说"第 30 秒"，而人声只有 4 秒 → 后端贴到末尾并在 skipped 里说明。
    // 面板不显示这个列表 = 用户以为它没生效，或者以为它真在 30 秒处。
    expect(hookSrc).toContain("setPremixNotes(r.skipped ?? [])")
    expect(panelSrc).toContain("sb.premixNotes.map")
  })

  it("★ sourceSeconds 只是看得见的范围，不许拿它封顶用户的输入", () => {
    expect(pageSrc).toContain("sourceSeconds={p.lastTts?.duration_s}")
    // 输入框不得带 max（封顶 = 默默改掉他填的数）；越界教训改成看得见的一行提醒。
    expect(panelSrc).not.toMatch(/max=\{sourceSeconds/)
    expect(panelSrc).toContain("sourceSeconds !== undefined")
  })

  it("★ 位置可以在波形上拖出来（填秒数只是精确手段，不是唯一入口）", () => {
    // 用户知道的是"说到那句的时候来一炮"——那是看在眼里的，不是数出来的。
    expect(panelSrc).toContain("<PremixTimeline")
    expect(panelSrc).toContain("onMove={sb.setPickAt}")
    // 波形画的是**源人声**（`at_s` 就是对着它的时间轴量的），不是混好的产物。
    expect(pageSrc).toContain('src={mediaUrl(p.lastTts?.url ?? "")}')
  })

  it("★ 一枚记号都没有时不解码（波形是懒加载的，不白拉整段 wav）", () => {
    const tl = readRel(path.join("pages", "Tts", "PremixTimeline.tsx"))
    expect(tl).toContain("const active = markers.length > 0")
    expect(tl).toContain("if (!active || !src) {")
    // 取不到波形时写出原因，**不是**一片空白（空白和"加载中"在界面上分不出来）。
    expect(tl).toContain("波形取不到")
  })
})

// ------------------------------------------------ 特效声板的主场景：实时变声页

describe("特效声板挂在实时变声页（主场景：边变声边打音效）", () => {
  const routeSrc = readRel(path.join("pages", "Live", "index.tsx"))
  const liveSrc = readRel(path.join("pages", "Live", "LivePage.tsx"))
  const cascadeSrc = readRel(path.join("pages", "Cascade", "CascadePage.tsx"))

  it("★ 声板按 sound.fx-board 门控；本页属 sound.rvc-live —— **不许**按它门控", () => {
    // 同 OfflineVc 前两个 tab 的判据：Live 路由由 `sound.rvc-live` 认领，关掉它整页都不存在，
    // 所以「宿主开着」在这一页恒真 —— 再按它门控只是把还能用的功能从界面上藏起来。
    expect(routeSrc).toContain('pluginVisible(catalog, "sound.fx-board")')
    expect(routeSrc).not.toContain('pluginVisible(catalog, "sound.rvc-live")')
    expect(routeSrc).toContain("useSoundboard(fxBoardOn)")
  })

  it("hook 只在路由层调一次，两个 tab 共用同一个实例", () => {
    // 各调一次 = 两回预热 + 两回目录请求，且两块的「正在播」高亮各说各话（Tts 页同一条判据）。
    expect(routeSrc.match(/useSoundboard\(/g)?.length).toBe(1)
    expect(routeSrc).toContain("<LivePage {...live} soundboard={soundboard} />")
    expect(routeSrc).toContain("<CascadePage {...qwen} soundboard={soundboard} />")
  })

  it("两个 tab 的控制台里各挂一块（RVC 实时与千问变声都是实时链路）", () => {
    expect(liveSrc.match(/<SoundboardPanel/g)?.length).toBe(1)
    expect(cascadeSrc.match(/<SoundboardPanel/g)?.length).toBe(1)
    expect(liveSrc).toContain("sb={p.soundboard}")
    expect(cascadeSrc).toContain("sb={p.soundboard}")
  })

  it("★ 实时页只给实时档，不给预混（这里没有「要混的那条合成产物」）", () => {
    // 预混的输入是一条 `tts_*.wav` 产物；实时链路里根本没有这一步，
    // 硬挂上去只会渲染出一个按下去必然 4xx 的按钮。
    expect(liveSrc).not.toContain("allowPremix")
    expect(cascadeSrc).not.toContain("allowPremix")
  })

  it("★ Live 页要说明「音效从 CABLE 出去」，否则用户以为点了没反应", () => {
    // 默认 hint 写的是"与人声一起被微信录走"；实时页的主场景是微信/游戏两可，
    // 而且开「自我监听」时自己耳机里也听得到（回环进程 tap 的正是 CABLE Output）。
    // 这句是「为什么我听不到」的唯一出口，所以钉住它。
    expect(liveSrc).toContain("开「自我监听」")
  })
})
