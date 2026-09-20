import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠（`web/electron/pet/pet.html`）的能力门控（2026-09-20 补）。
 *
 * 为什么需要：桌宠请求的接口里有 3 个属于**可关能力** —— 关掉之后后端根本不挂载
 * 那些 router（实测 `mount_plan()` 的差异）：
 *   - 关 `pet.market`      → `pet_market_api` 不挂载
 *   - 关 `sound.rvc-live`  → `rvc_live` + `cascade` 不挂载
 *   - 关 `hook.wechat`     → `wechat_voice` 不挂载
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
  const all = ['core.system', 'core.voices', 'pet.market', 'sound.rvc-live', 'hook.wechat']
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
  it('全启用时三处都开着', () => {
    expect(capStateFromCatalog(catalogWithDisabled([]))).toEqual({
      market: true,
      live: true,
      wechat: true,
    })
  })

  it.each([
    ['pet.market', 'market'],
    ['sound.rvc-live', 'live'],
    ['hook.wechat', 'wechat'],
  ])('关掉 %s → 只有 %s 被关', (id, key) => {
    const s = capStateFromCatalog(catalogWithDisabled([id]))
    expect(s[key], `${id} 关了，${key} 应该是 false`).toBe(false)
    // 关一个不该牵连别的 —— 否则「关掉市场」会把桌宠的变声也一起弄没
    for (const other of ['market', 'live', 'wechat']) {
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

  it('三处调用点各自有门控', () => {
    expect(html).toContain('if (!CAP.market) return;') // loadSkin：皮肤接口
    expect(html).toContain('if (!CAP.wechat) return;') // loadRecent：最近发送
    expect(html).toContain('refreshCapVisibility()')
  })

  it('收起来的控件正好是那三处', () => {
    const fn = html.slice(html.indexOf('function refreshCapVisibility()'))
    expect(fn.slice(0, 600)).toContain('hide("engRow", !CAP.live)')
    expect(fn.slice(0, 600)).toContain('hide("live", !CAP.live)')
    expect(fn.slice(0, 600)).toContain('hide("recent", !CAP.wechat)')
  })

  it('★ 清单必须先到位再轮询（否则第一帧就去打被关掉的接口）', () => {
    expect(html).toContain('await loadCapabilities()')
    // loadSkin 的 setInterval 也必须在清单之后、且带 CAP.market 条件
    expect(html).toContain('if (!MOCK && CAP.market)')
  })
})
