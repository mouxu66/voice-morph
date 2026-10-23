import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠「该不该回」雷达层（`web/electron/pet/pet.html`，2026-10-23 补）。
 *
 * 为什么不是在 HTML 里 grep 字符串：上一版测试只断言「某行代码存在」，
 * 于是 `radarSigRows` 把 `null` 写成 `0s`、`radarSetPolling` 换了 hint 不重建定时器，
 * 两条都能全绿。这里把**真实函数抽出来跑**（与 petCapabilityGate 同一套沙箱做法），
 * 数字是算出来的，不是数行数。
 *
 * 抽函数而不是手写副本：pet.html 里改坏了，这里必须红。
 */

const html = readFileSync(path.join(process.cwd(), 'electron', 'pet', 'pet.html'), 'utf8')

/** 按花括号配平，从 pet.html 里抠出一个顶层函数源码。 */
function extractFn(name: string): string {
  const start = html.indexOf(`function ${name}(`)
  expect(start, `pet.html 里找不到 function ${name}`).toBeGreaterThan(-1)
  let depth = 0
  for (let i = html.indexOf('{', start); i < html.length; i++) {
    if (html[i] === '{') depth++
    else if (html[i] === '}' && --depth === 0) return html.slice(start, i + 1)
  }
  throw new Error(`${name} 花括号不配平`)
}

const SIGNAL_SRC = `${extractFn('radarSecs')}\n${extractFn('radarSigRows')}`

type Rows = Array<[string, string]>
const sigRows = new Function(`${SIGNAL_SRC}\nreturn radarSigRows;`)() as (s: Record<string, unknown>) => Rows

/** 造一个 radarSetPolling 的沙箱：setInterval/clearInterval 被记账，不真起定时器。 */
function loadPolling() {
  const created: Array<{ id: number; ms: number }> = []
  const cleared: number[] = []
  let seq = 1
  // const：真正的重新赋值发生在 new Function 内部的 apply（那是它的参数），
  // 这个外层变量只被读一次（喂给沙箱）。写成 let 会被 prefer-const 拦。
  const env: { layer: string; cap: boolean } = { layer: '', cap: true }

  const src = extractFn('radarSetPolling')
  const body = `
    let panelLayer = env.layer;
    const CAP = { radar: env.cap };
    let radarTimer = null;
    let radarTimerMs = 0;
    let radarBadgeTimer = null;
    let radarPollHintMs = 1500;
    const radarTick = () => {};
${src}
    return {
      set: radarSetPolling,
      setHint: (v) => { radarPollHintMs = v },
      apply: (next) => { env = next; panelLayer = next.layer; CAP.radar = next.cap; },
      state: () => ({ timer: radarTimer, ms: radarTimerMs, badge: radarBadgeTimer }),
    };
  `
  const api = new Function(
    'env',
    'setInterval',
    'clearInterval',
    body,
  )(env, (_fn: unknown, ms: number) => {
    created.push({ id: seq, ms })
    return seq++
  }, (id: number) => { cleared.push(id) }) as {
    set: () => void
    setHint: (v: number) => void
    apply: (next: { layer: string; cap: boolean }) => void
    state: () => { timer: number | null; ms: number; badge: number | null }
  }

  return { api, created, cleared }
}

describe('雷达层：信号行的数值不许凭空造', () => {
  it('对方最后说话且 gap 已知：给出秒数', () => {
    const rows = sigRows({ last_side: 'left', gap_seconds: 42, gap_lower_bound: false, streak: 1, frames_seen: 7 })
    expect(rows).toContainEqual(['对方最后发言', '42s 前'])
  })

  it('冷启动的下界要带「≥」——那是「至少」，不是精确值', () => {
    const rows = sigRows({ last_side: 'left', gap_seconds: 12, gap_lower_bound: true, streak: 1, frames_seen: 2 })
    expect(rows).toContainEqual(['对方最后发言', '≥12s 前'])
  })

  it('★ gap 为 null 时不能写成 0s 前（那是编出来的「刚发」）', () => {
    const rows = sigRows({ last_side: 'left', gap_seconds: null, gap_lower_bound: false, streak: 1, frames_seen: 1 })
    expect(rows).toContainEqual(['对方最后发言', '—'])
    expect(JSON.stringify(rows)).not.toContain('0s')
  })

  it('★ 我最后说话但还没观测过发送时刻：等对方回要显示「—」而不是 0s', () => {
    const rows = sigRows({ last_side: 'right', my_last_reply_age: null, streak: 0, frames_seen: 1 })
    expect(rows).toContainEqual(['等对方回', '—'])
    expect(JSON.stringify(rows)).not.toContain('0s')
  })

  it('★ 一个消息块都没识别到时，不许声称「最后一条 你说的」', () => {
    const rows = sigRows({ last_side: null, streak: 0, frames_seen: 3 })
    expect(rows).toContainEqual(['最后一条', '没看到消息'])
    expect(JSON.stringify(rows)).not.toContain('你说的')
  })

  it('连发 ≥2 条才单独列一行（1 条是常态，列表里不需要）', () => {
    const one = sigRows({ last_side: 'left', gap_seconds: 5, gap_lower_bound: false, streak: 1, frames_seen: 2 })
    const three = sigRows({ last_side: 'left', gap_seconds: 5, gap_lower_bound: false, streak: 3, frames_seen: 2 })
    expect(one.some(([k]) => k === '连发')).toBe(false)
    expect(three).toContainEqual(['连发', '3 条'])
  })
})

describe('雷达层：轮询定时器', () => {
  it('层开着时起快轮询 + 常驻慢轮询', () => {
    const { api, created } = loadPolling()
    api.apply({ layer: 'radar', cap: true })
    api.set()
    expect(created.map((c) => c.ms)).toEqual([1500, 5000])
  })

  it('★ 后端改了 poll_hint_ms：定时器必须重建，否则新节奏永远不生效', () => {
    const { api, created, cleared } = loadPolling()
    api.apply({ layer: 'radar', cap: true })
    api.set()
    const first = created[0]
    api.setHint(800)
    api.set()
    // 老定时器被清掉、新定时器用 800
    expect(cleared).toContain(first.id)
    expect(created.at(-1)?.ms).toBe(800)
    expect(api.state().ms).toBe(800)
  })

  it('hint 没变时不重建（否则每帧都会重起一个定时器）', () => {
    const { api, created, cleared } = loadPolling()
    api.apply({ layer: 'radar', cap: true })
    api.set()
    api.set()
    api.set()
    expect(created).toHaveLength(2) // 快 + 慢各一个
    expect(cleared).toHaveLength(0)
  })

  it('层关掉：快轮询停、慢轮询留着养绿点', () => {
    const { api, cleared } = loadPolling()
    api.apply({ layer: 'radar', cap: true })
    api.set()
    api.apply({ layer: '', cap: true })
    api.set()
    const st = api.state()
    expect(st.timer).toBeNull()
    expect(st.badge).not.toBeNull()
    expect(cleared).toHaveLength(1)
  })

  it('★ 能力关掉：两档轮询全停（后端没那些接口，轮询只会 404）', () => {
    const { api, cleared } = loadPolling()
    api.apply({ layer: 'radar', cap: true })
    api.set()
    api.apply({ layer: 'radar', cap: false })
    api.set()
    const st = api.state()
    expect(st.timer).toBeNull()
    expect(st.badge).toBeNull()
    expect(cleared).toHaveLength(2)
  })
})
