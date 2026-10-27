import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠「怎么回」主卡 + 换身材（2026-10-27）。
 *
 * 这一版把「怎么回」从建议区里的一格 chip 提成**常驻主卡**（#replyCard），点进去
 * 面板原地长大到 440×640 的两栏工作台。所以有两件事必须钉住，都是「看起来能用、
 * 其实在骗人」的那类：
 *
 *   ① 主卡**不造判断**。它只有 lastRadar 一个真相源 —— 没拿到结论就老实写用法，
 *      绝不能为了「让卡片好看」编一句「该回了」。
 *   ② 主卡副行给的是一句**真能发出去**的话（来自草稿），过期（90s）就撤掉：
 *      群里过了两分钟，那句话已经不对应当前这句了。
 *
 * 换身材那一段单独一组：窗口尺寸只有主进程能改，「该不该大」只有渲染层知道，
 * 两边必须一起动 —— 只变 CSS 会把 424px 的面板挤进 220px 窗口（右边整块被切）。
 */

const html = readFileSync(path.join(process.cwd(), 'electron', 'pet', 'pet.html'), 'utf8')

/** 按花括号配平，抠出一个顶层函数源码。 */
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

/** 最小元素：textContent / className / classList（add/remove 记进集合，便于断言）。 */
function fakeEl() {
  const el = {
    textContent: '',
    className: '',
    hidden: false,
    style: {} as Record<string, string>,
    classes: new Set<string>(),
    classList: {
      add(c: string) { el.classes.add(c) },
      remove(c: string) { el.classes.delete(c) },
      toggle(c: string, on?: boolean) {
        const want = on === undefined ? !el.classes.has(c) : !!on
        if (want) el.classes.add(c)
        else el.classes.delete(c)
        return want
      },
      contains(c: string) { return el.classes.has(c) },
    },
  }
  return el
}

type CardInit = {
  lastRadar?: unknown
  candRows?: string[]
  candAt?: number
  candPending?: boolean
}

function loadCard(init: CardInit) {
  const verdict = fakeEl()
  const sub = fakeEl()
  const body = `
    let lastRadar = init.lastRadar || null;
    let candRows = init.candRows || [];
    let candAt = init.candAt || 0;
    let candPending = !!init.candPending;
    const rcVerdictEl = dom.verdict;
    const rcSubEl = dom.sub;
    ${extractFn('lastRadarAction')}
    ${extractFn('candFresh')}
    ${extractFn('renderReplyCard')}
    return { render: renderReplyCard, candFresh, lastRadarAction };
  `
  const api = new Function('init', 'dom', body)(init, { verdict, sub }) as {
    render: () => void
    candFresh: () => boolean
    lastRadarAction: () => string
  }
  return { ...api, verdict, sub }
}

const radar = (action: string) => ({ verdict: { action } })

describe('怎么回主卡：标题只报雷达结论', () => {
  it.each([
    ['now', '该回了', 'now'],
    ['soon', '可以回了', ''],
    ['wait', '再等等', ''],
    ['none', '不用回', ''],
  ])('action=%s → 「%s」', (action, label, cls) => {
    const c = loadCard({ lastRadar: radar(action) })
    c.render()
    expect(c.verdict.textContent).toBe(label)
    expect(c.verdict.className).toBe(cls)
  })

  it('★ 没拿到雷达结论时一个判断都不写（宁可空着，也不编「该回了」）', () => {
    for (const init of [{}, { lastRadar: null }, { lastRadar: {} }, { lastRadar: { verdict: {} } }]) {
      const c = loadCard(init)
      c.render()
      expect(c.verdict.textContent).toBe('')
      expect(c.verdict.className).toBe('')
    }
  })

  it('认不出的 action 也不硬套一档', () => {
    const c = loadCard({ lastRadar: radar('whatever') })
    c.render()
    expect(c.verdict.textContent).toBe('')
    expect(c.verdict.className).toBe('')
  })
})

describe('怎么回主卡：副行给一句能直接发的话', () => {
  const now = Date.now()

  it('有新鲜草稿 → 整句上屏（这里宽，不做 9 字截断）', () => {
    const c = loadCard({
      lastRadar: radar('now'),
      candRows: ['哈哈你吃这么快，红包我先收下了啊'],
      candAt: now,
    })
    c.render()
    expect(c.sub.textContent).toBe('哈哈你吃这么快，红包我先收下了啊')
    expect(c.sub.classes.has('cand')).toBe(true)
    // 结论与草稿同时在场：标题说「该回」，副行说「回什么」
    expect(c.verdict.textContent).toBe('该回了')
  })

  it('★ 草稿过期（90s）就撤掉 —— 群里过了两分钟，那句已经不对应当前这句了', () => {
    const c = loadCard({
      lastRadar: radar('now'),
      candRows: ['刚才那句'],
      candAt: now - 91_000,
    })
    c.render()
    expect(c.sub.textContent).not.toContain('刚才那句')
    expect(c.sub.textContent).toContain('读当前会话')
    expect(c.sub.classes.has('cand')).toBe(false)
  })

  it('89s 算新鲜（边界不能提前一秒撤）', () => {
    const c = loadCard({ candRows: ['还在有效期内'], candAt: now - 89_000 })
    expect(c.candFresh()).toBe(true)
    c.render()
    expect(c.sub.textContent).toBe('还在有效期内')
  })

  it('草稿是空白字符串时当没有（不然副行会变成一片空白）', () => {
    const c = loadCard({ candRows: ['   '], candAt: now })
    c.render()
    expect(c.sub.textContent).toContain('读当前会话')
    expect(c.sub.classes.has('cand')).toBe(false)
  })

  it('正在拉草稿 → 说「正在读当前会话…」，别让用户以为没反应', () => {
    const c = loadCard({ candPending: true })
    c.render()
    expect(c.sub.textContent).toBe('正在读当前会话…')
  })

  it('拿到草稿后 candPending 不再抢话（草稿优先）', () => {
    const c = loadCard({ candRows: ['来了'], candAt: Date.now(), candPending: true })
    c.render()
    expect(c.sub.textContent).toBe('来了')
  })
})

describe('怎么回：换身材（220×480 → 440×640）', () => {
  function loadBig() {
    const panel = fakeEl()
    const root = fakeEl()
    const calls: boolean[] = []
    const body = `
      let panelOpen = true;
      let panelBig = false;
      const panel = dom.panel;
      const rootEl = dom.root;
      ${extractFn('syncPanelDisplay')}
      ${extractFn('setPanelBig')}
      return { set: setPanelBig, sync: syncPanelDisplay, open: (v) => { panelOpen = v } };
    `
    const api = new Function('dom', 'window', body)(
      { panel, root },
      { pet: { setBig: (on: boolean) => calls.push(on) } },
    ) as { set: (on: boolean) => void; sync: () => void; open: (v: boolean) => void }
    return { ...api, panel, root, calls }
  }

  it('放大：面板挂 .big、根挂 .panel-big，并通知主进程改窗口', () => {
    const b = loadBig()
    b.set(true)
    expect(b.panel.classes.has('big')).toBe(true)
    expect(b.root.classes.has('panel-big')).toBe(true)
    expect(b.calls).toEqual([true])
  })

  it('缩回：三个都还原（少还原一个就会「窗口小了但面板还是 424px」）', () => {
    const b = loadBig()
    b.set(true)
    b.set(false)
    expect(b.panel.classes.has('big')).toBe(false)
    expect(b.root.classes.has('panel-big')).toBe(false)
    expect(b.calls).toEqual([true, false])
  })

  it('★ 幂等：同一个状态连调两次只通知一次', () => {
    const b = loadBig()
    b.set(true)
    b.set(true)
    b.set(true)
    expect(b.calls).toEqual([true])
  })

  it('display 只有一处写：小面板 block、大面板 flex（两栏要分剩余高度）', () => {
    const b = loadBig()
    b.sync()
    expect(b.panel.style.display).toBe('block')
    b.set(true)
    expect(b.panel.style.display).toBe('flex')
    b.open(false)
    b.sync()
    expect(b.panel.style.display).toBe('none')
  })

  it('浏览器里单开 pet.html（没有 window.pet）也不能抛错', () => {
    const body = `
      let panelOpen = true;
      let panelBig = false;
      const panel = dom.panel;
      const rootEl = null;   // 顺手把「没有 #root」也过一遍
      ${extractFn('syncPanelDisplay')}
      ${extractFn('setPanelBig')}
      return setPanelBig;
    `
    const set = new Function('dom', 'window', body)({ panel: fakeEl() }, {}) as (on: boolean) => void
    expect(() => set(true)).not.toThrow()
  })

  it('★ 只有「怎么回」那一层配大窗口（托盘 141px 在 466px 里会显得空）', () => {
    expect(html).toContain('setPanelBig(panelLayer === "ana")')
    // 全文件只有这一处调用，别处再调就会出现「窗口大了但内容是托盘」
    const calls = html.match(/setPanelBig\(/g) || []
    // 1 处调用 + 1 处定义 + 注释里提到的算 0（定义里没有左括号）
    expect(calls.length).toBe(2)
  })
})
