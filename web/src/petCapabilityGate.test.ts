import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠（`web/electron/pet/pet.html`）的能力门控（2026-09-20 补）。
 *
 * 为什么需要：桌宠请求的接口里有 4 个属于**可关能力** —— 关掉之后后端根本不挂载
 * 那些 router（实测 `mount_plan()` 的差异）：
 *   - 关 `pet.market`      → `pet_market_api` 不挂载
 *   - 关 `sound.rvc-live`  → `rvc_live` + `cascade` 不挂载
 *   - 关 `hook.wechat`     → `wechat_voice` 不挂载
 *   - 关 `sound.fx-board`  → `soundboard` 不挂载（2026-09-26 面板加「音效」页时纳入）
 * 而桌宠原本对能力清单一无所知（全文没有一处读 `/api/plugins`）。
 *
 * 其中最严重的一条：**`tick()` 拿「cascade + live 都拉不到」当后端离线的判据** ——
 * 一旦关掉 `sound.rvc-live`，这两个接口同时消失，桌宠会**永久显示「后端离线」**，
 * 而后端其实好好的。
 *
 * 这些断言读的是**真实的 pet.html**（门控块用 `===CAP-GATE-START/END===` 标出），
 * 不手写 fixture —— 否则文件改了测试照样绿。
 */

const webRoot = process.cwd()
const petHtml = path.join(webRoot, 'electron', 'pet', 'pet.html')
const html = readFileSync(petHtml, 'utf8')

const START = '// ===CAP-GATE-START==='
const END = '// ===CAP-GATE-END==='

function gateBlock(): string {
  const a = html.indexOf(START)
  const b = html.indexOf(END)
  return a >= 0 && b > a ? html.slice(a + START.length, b) : ''
}

type Catalog = { plugins: Array<Record<string, unknown>> } | null | undefined

/** 在沙箱里跑真实的门控片段，取回它的判定函数。 */
function loadGate(): { capStateFromCatalog: (c: Catalog) => Record<string, boolean> } {
  const src = gateBlock()
  // 标记丢了 → 下面所有断言都会以「空字符串」的方式诡异地过/不过，先钉住。
  expect(src.length, `pet.html 里找不到 ${START} … ${END} 标记`).toBeGreaterThan(0)
  const fn = new Function(
    `${src}\nreturn { capStateFromCatalog: typeof capStateFromCatalog === "function" ? capStateFromCatalog : null };`,
  )
  const got = fn() as { capStateFromCatalog: ((c: Catalog) => Record<string, boolean>) | null }
  expect(got.capStateFromCatalog, '门控块里必须定义 capStateFromCatalog').toBeTypeOf('function')
  return got as { capStateFromCatalog: (c: Catalog) => Record<string, boolean> }
}

const { capStateFromCatalog } = loadGate()

/** 造一个 /api/plugins 形状的目录；ids 里列出的能力标记为已关闭。 */
function catalogWithDisabled(ids: string[]): Catalog {
  const all = ['core.system', 'core.voices', 'pet.market', 'sound.rvc-live', 'hook.wechat', 'sound.fx-board']
  return {
    plugins: all.map((id) => ({
      id,
      core: id.startsWith('core.'),
      state: ids.includes(id) ? 'disabled' : 'ok',
      enabled: !ids.includes(id),
    })),
  }
}

describe('桌宠的能力门控', () => {
  it('全启用时四处都开着', () => {
    expect(capStateFromCatalog(catalogWithDisabled([]))).toEqual({
      market: true,
      live: true,
      wechat: true,
      fx: true,
    })
  })

  it.each([
    ['pet.market', 'market'],
    ['sound.rvc-live', 'live'],
    ['hook.wechat', 'wechat'],
    ['sound.fx-board', 'fx'],
  ])('关掉 %s → 只有 %s 被关', (id, key) => {
    const s = capStateFromCatalog(catalogWithDisabled([id]))
    expect(s[key], `${id} 关了，${key} 应该是 false`).toBe(false)
    // 关一个不该牵连别的 —— 否则「关掉市场」会把桌宠的变声也一起弄没
    for (const other of ['market', 'live', 'wechat', 'fx']) {
      if (other !== key) expect(s[other]).toBe(true)
    }
  })

  it('★ 取不到清单时一律当作开着（别把桌宠弄成残废）', () => {
    // 后端没起来 / 清单接口自己出错 —— 这时「猜错了」的代价远大于「没藏」：
    // 前者让桌宠看起来坏了，后者只是保留原样。
    for (const bad of [null, undefined, {}, { plugins: 'oops' }, { plugins: [] }]) {
      expect(capStateFromCatalog(bad as Catalog), `输入 ${JSON.stringify(bad)}`).toEqual({
        market: true,
        live: true,
        wechat: true,
        fx: true,
      })
    }
  })

  it('★ 只看 enabled，不看 state（被依赖而保留的能力不能藏）', () => {
    // 被别的启用中能力依赖时是 `state=disabled / enabled=true`，路由**仍然挂着**。
    // 拿 state 判会把还在的能力藏掉 —— 这正是主界面 `isVisible` 踩过的坑。
    const c: Catalog = { plugins: [{ id: 'sound.rvc-live', state: 'disabled', enabled: true }] }
    expect(capStateFromCatalog(c).live).toBe(true)
  })

  it('清单里有不认识的 id 不影响判定', () => {
    const c: Catalog = { plugins: [{ id: 'sound.tts', enabled: false }, { id: 'pet.market', enabled: false }] }
    const s = capStateFromCatalog(c)
    expect(s.market).toBe(false)
    expect(s.live).toBe(true)
    expect(s.wechat).toBe(true)
    expect(s.fx).toBe(true)
  })
})

describe('门控真的接到了调用点上（不是写了个没人用的函数）', () => {
  it('tick() 不再拿已关闭能力的接口判「后端离线」', () => {
    // 关掉 sound.rvc-live 后 cascade/live 都不存在，必须改问 core.system 的 /api/health
    expect(html).toContain('if (CAP.live) {')
    expect(html).toContain('HEALTH_API')
    // 而且 /api/health 只在 CAP.live 关掉时才作为离线判据出现
    const tickBody = html.slice(html.indexOf('async function tick()'), html.indexOf('// 谁在跑'))
    expect(tickBody).toContain('HEALTH_API')
  })

  it('四处调用点各自有门控', () => {
    expect(html).toContain('if (!CAP.market) return;') // loadSkin：皮肤接口
    expect(html).toContain('if (!CAP.wechat) return;') // loadRecent：最近发送
    expect(html).toContain('if (!CAP.fx) return;')     // loadSfx：音效声板
    expect(html).toContain('refreshCapVisibility()')
  })

  it('收起来的控件正好是那六处', () => {
    // 窗口给足以至于加了注释也不会假红：以前是死写 600，而门控函数里现在有一段
    // 解释"为什么试听/发送也要收"的注释，离得稍远就把断言挤出去。
    // （2026-09-26 面板去重：live 按钮并入引擎分段，「hide("live")」随之删除，5→4 处。
    //   2026-09-26 音效页：「音效」整体归 sound.fx-board，tabRow + fxPane 两处 hide，4→6 处。）
    const fn = html.slice(html.indexOf('function refreshCapVisibility()'), html.indexOf('function refreshCapVisibility()') + 1600)
    for (const call of [
      'hide("engRow", !CAP.live)',
      'hide("recent", !CAP.wechat)',
      'hide("preview", !CAP.wechat)',
      'hide("send", !CAP.wechat)',
      'hide("tabRow", !CAP.fx)',
      'hide("fxPane", !CAP.fx)',
    ]) {
      expect(fn, `refreshCapVisibility 漏了 ${call}`).toContain(call)
    }
  })

  it('★ 试听/发送两个按钮也归 hook.wechat 管（否则关了能力就是一按 404）', () => {
    // 两者的后端都是 wechat_voice.py 里的路由，跟「最近发送」同一个插件。
    // 试听按钮正是因为这个才曾经对市场音色变成死钮 —— 现在补了不发送的端点，
    // 但端点仍归 hook.wechat，所以可见性必须跟着 CAP.wechat 走。
    expect(html).toContain('id="preview"')
    expect(html).toContain('hide("preview", !CAP.wechat)')
    // 点了也要能自辩（按钮可能因 CSS/缓存没被藏住）
    const body = html.slice(html.indexOf('function doPreview()'), html.indexOf('function doSendText()'))
    expect(body).toContain('if (!CAP.wechat)')
  })

  it('★ 音效页整体归 sound.fx-board（关了能力就是一页 404）', () => {
    // loadSfx 打的是 /api/soundboard/*（sound.fx-board 插件的路由）：
    // 关掉后 catalog/play/premix 全部 404，所以入口和页体必须一起收，
    // 且残留的 tab=fx 要被归位回「说话」页。
    expect(html).toContain('id="tabRow"')
    expect(html).toContain('id="fxPane"')
    const loadSfxBody = html.slice(html.indexOf('async function loadSfx('), html.indexOf('function renderFxGrid'))
    expect(loadSfxBody).toContain('if (!CAP.fx) return;')
    expect(html).toContain('if (!CAP.fx && activeTab === "fx") setTab("speak", false);')
  })

  it('★ 音效页的发送走 premix → sendWav 既有链路（不新造发送路径）', () => {
    // premix 产物必须经 window.pet.sendWav 发出（与「发送试听」同一条 IPC 链），
    // skipped 必须写进状态行 —— 静默跳过 = 发出去的语音里少了一声，界面还一切正常。
    const body = html.slice(html.indexOf('async function premixAndSend()'), html.indexOf('previewBtn.addEventListener'))
    expect(body).toContain('SFX_BASE + "/premix"')
    expect(body).toContain('window.pet.sendWav(j.wav)')
    expect(body).toContain('j.skipped')
  })

  it('★ 清单必须先到位再轮询（否则第一帧就去打被关掉的接口）', () => {
    expect(html).toContain('await loadCapabilities()')
    // loadSkin 的 setInterval 也必须在清单之后、且带 CAP.market 条件
    expect(html).toContain('if (!MOCK && CAP.market)')
  })
})
