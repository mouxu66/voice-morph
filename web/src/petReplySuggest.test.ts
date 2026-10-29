import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠「怎么回」草稿块（`web/electron/pet/pet.html`，2026-10-24 补）。
 *
 * 这一块的职责是**把后端给的 2-3 条草稿画成可点按钮**，点一下填进输入框。
 * 与雷达层同一套做法：把真实函数从 pet.html 抠出来跑，而不是 grep 字符串 ——
 * 「某行代码存在」不能证明它算出的是对的。
 *
 * 易错点（每条都对应下面的用例）：
 *   - `last_side` 只认 left/right，其他值透传会让模型误判「最后一条是我发的」；
 *   - 连点「重新分析 / 换一批」时旧响应必须作废，否则用户看到的是上一轮的草稿；
 *   - 模型没起来时后端降级成模板，前端必须把「这是模板」说出来；
 *   - 草稿出来要收掉原消息列表与要点 —— 面板 300px 是死预算。
 */

const html = readFileSync(path.join(process.cwd(), 'electron', 'pet', 'pet.html'), 'utf8')

/** 按花括号配平，从 pet.html 里抠出一个顶层函数源码（`async` 前缀要一起带上，
 *  否则 await 会变成「不在 async 函数里」的语法错误）。 */
function extractFn(name: string): string {
  const plain = `function ${name}(`
  const asyncAt = html.indexOf(`async ${plain}`)
  const start = asyncAt >= 0 ? asyncAt : html.indexOf(plain)
  expect(start, `pet.html 里找不到 function ${name}`).toBeGreaterThan(-1)
  let depth = 0
  for (let i = html.indexOf('{', start); i < html.length; i++) {
    if (html[i] === '{') depth++
    else if (html[i] === '}' && --depth === 0) return html.slice(start, i + 1)
  }
  throw new Error(`${name} 花括号不配平`)
}

/** 极简 classList：真页面上 `anaRepliesEl.classList.add("skel")` / `.remove(...)` 都会走到。
 *  以前的假元素没有它 —— `suggestReplies` 在骨架那一步就抛 TypeError，于是整个文件
 *  的断言全部「红着但看不出原因」。 */
function fakeClassList() {
  const set = new Set<string>()
  return {
    hot: false,
    add(n: string) { set.add(n) },
    remove(n: string) { set.delete(n) },
    contains(n: string) { return set.has(n) },
    toggle(n: string, on?: boolean) {
      const v = on === undefined ? !set.has(n) : !!on
      if (v) set.add(n)
      else set.delete(n)
      if (n === 'hot') this.hot = v
      return v
    },
  }
}

function fakeEl() {
  return { innerHTML: '', textContent: '', className: '', hidden: false, classList: fakeClassList() }
}

type ReplyPayload = {
  ok?: boolean
  engine?: string
  /** 后端 `suggest()` 会回报实际用的模型名（页面暂不展示，但夹具按真实响应写） */
  model?: string
  replies?: string[]
  why?: string
  note?: string
  error?: string
}

type FakeResponse = { ok: boolean; json: () => Promise<ReplyPayload> }

/** 把草稿块用到的真实函数装进沙箱；fetch 由用例注入，不真发请求。 */
function loadReplyPanel(fetchImpl: (url: string) => Promise<FakeResponse>) {
  const dom = {
    replies: fakeEl(),
    note: fakeEl(),
    msgs: fakeEl(),
    list: fakeEl(),
    title: fakeEl(),
    suggest: fakeEl(),
    // ↓ 2026-10-27 补齐：这几条在真实页面里是常驻 DOM / 并行模块，
    //   不声明的话 renderAnalysis 第一行就 ReferenceError，测试会「假通过」
    //   （异常被 suggestReplies 的 catch 吞成一行提示，断言只看 hidden 就看不出来）。
    digest: fakeEl(),
    card: fakeEl(),
    reply: fakeEl(),
    searchBtn: { classList: fakeClassList() },
  }
  const env = { cap: true }
  const body = `
    let anaReplySeq = 0;
    let anaLast = null;
    let anaLastReplies = [];
    // 读多少屏（面板头部那个范围选择器）。沙箱默认 0 = 只读当前屏。
    let anaScreens = 0;
    /** 骨架是纯视觉（三条无字占位），这里只要不炸；真实样式由面板布局用例管。 */
    function anaSkeleton() { dom.replies.innerHTML = ""; }
    // 主面板那一行有没有「15s 内的现成草稿」——沙箱里恒为 0，逼它走真请求那条路。
    let candRows = [];
    let candAt = 0;
    const applied = [];
    const CAP = { radar: env.cap };
    const RADAR_BASE = "/api/radar";
    const anaRepliesEl = dom.replies;
    const anaReplyNoteEl = dom.note;
    const anaMsgsEl = dom.msgs;
    const anaListEl = dom.list;
    const anaTitleEl = dom.title;
    const anaSuggestEl = dom.suggest;
    const anaNoteEl = dom.note;
    const anaDigestEl = dom.digest;
    const anaCardEl = dom.card;
    const anaReplyEl = dom.reply;
    const anaSearchBtnEl = dom.searchBtn;
    const ANA_KIND_LABEL = { media: "图片/文件", system: "系统" };
    /** 草稿同时要喂给主面板那一行（#replyCard 的行尾 + 点一下就用）。这里只记调用。 */
    function applyCand(j) { applied.push(j); }
    /** 认名提示 / 攻略块：各有一套独立用例，这里只要不炸。 */
    function anaMarkNamesHint() {}
    function clearDigest() {}
    function renderDigest() {}
    ${extractFn('esc')}
    ${extractFn('clearReplies')}
    ${extractFn('renderReplies')}
    ${extractFn('suggestReplies')}
    ${extractFn('anaMsgRow')}
    ${extractFn('anaMsgRows')}
    ${extractFn('anaWho')}
    ${extractFn('renderAnalysis')}
    return {
      suggest: suggestReplies,
      render: renderAnalysis,
      clear: clearReplies,
      dom,
      applied: () => applied,
      setCap: (v) => { CAP.radar = v },
      setScreens: (v) => { anaScreens = v },
      last: () => anaLast,
    };
  `
  return new Function('env', 'dom', 'fetch', body)(env, dom, fetchImpl) as {
    suggest: (j?: Record<string, unknown>) => Promise<void>
    render: (j: Record<string, unknown>, autoReply?: boolean) => void
    clear: () => void
    /** 模拟用户在面板头部切了范围（0 / 2 / 4 屏） */
    setScreens: (v: number) => void
    dom: typeof dom
    /** applyCand 收到的每一份响应：证明草稿也喂给了主面板那一行 */
    applied: () => ReplyPayload[]
    setCap: (v: boolean) => void
    last: () => Record<string, unknown> | null
  }
}

/** 一个可手动 resolve 的 promise，用来构造「旧请求后到」。 */
function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

const OK: ReplyPayload = {
  ok: true,
  engine: 'local',
  model: 'qwen3vl-4b',
  replies: ['哈哈你吃这么快？', '红包我先收下了'],
  why: '对方发了语音和图片，想回应互动',
}

function okResponse(payload: ReplyPayload): FakeResponse {
  return { ok: true, json: async () => payload }
}

describe('怎么回：请求参数', () => {
  it('默认只带 limit=14 与 screens=0（没有信号就不猜最后一条是谁发的）', async () => {
    const urls: string[] = []
    const api = loadReplyPanel(async (url) => {
      urls.push(url)
      return okResponse(OK)
    })
    await api.suggest({})
    expect(urls).toEqual(['/api/radar/replies?limit=14&screens=0'])
  })

  it('用户选了范围就把 screens 原样带上（读多少由他决定）', async () => {
    const urls: string[] = []
    const api = loadReplyPanel(async (url) => {
      urls.push(url)
      return okResponse(OK)
    })
    api.setScreens(3)
    await api.suggest({})
    expect(urls[0]).toBe('/api/radar/replies?limit=14&screens=3')
  })

  it('雷达给了 left/right 才透传 last_side', async () => {
    const urls: string[] = []
    const api = loadReplyPanel(async (url) => {
      urls.push(url)
      return okResponse(OK)
    })
    await api.suggest({ signals: { last_side: 'left' } })
    await api.suggest({ signals: { last_side: 'right' } })
    expect(urls[0]).toBe('/api/radar/replies?limit=14&screens=0&last_side=left')
    expect(urls[1]).toBe('/api/radar/replies?limit=14&screens=0&last_side=right')
  })

  it('★ 非法 last_side 不透传（模型会把「最后一条」判反）', async () => {
    const urls: string[] = []
    const api = loadReplyPanel(async (url) => {
      urls.push(url)
      return okResponse(OK)
    })
    await api.suggest({ signals: { last_side: 'middle' } })
    expect(urls[0]).toBe('/api/radar/replies?limit=14&screens=0')
    expect(urls[0]).not.toContain('last_side')
  })

  it('能力关掉时一个请求都不发（后端根本没有 /api/radar/*）', async () => {
    let calls = 0
    const api = loadReplyPanel(async () => {
      calls++
      return okResponse(OK)
    })
    api.setCap(false)
    await api.suggest({})
    expect(calls).toBe(0)
  })
})

describe('怎么回：渲染', () => {
  it('等待时说明要拉起模型，而不是空白块', async () => {
    const d = deferred<FakeResponse>()
    const api = loadReplyPanel(() => d.promise)
    const p = api.suggest({})
    expect(api.dom.note.textContent).toContain('本地模型在想怎么回')
    expect(api.dom.replies.innerHTML).toBe('')
    d.resolve(okResponse(OK))
    await p
  })

  it('每条草稿都是可点按钮，文本要转义', async () => {
    const api = loadReplyPanel(async () => okResponse({ ...OK, replies: ['<b>别急</b>', '好嘞'] }))
    await api.suggest({})
    expect(api.dom.replies.innerHTML).toContain('class="rchip"')
    expect(api.dom.replies.innerHTML).toContain('&lt;b&gt;别急&lt;/b&gt;')
    expect(api.dom.replies.innerHTML).not.toContain('<b>别急</b>')
    expect((api.dom.replies.innerHTML.match(/class="rchip"/g) || []).length).toBe(2)
    expect(api.dom.note.textContent).toBe(OK.why)
    expect(api.dom.note.className).toBe('')
  })

  it('★ 草稿出来就收掉原消息列表与要点（300px 是死预算）', async () => {
    const api = loadReplyPanel(async () => okResponse(OK))
    api.dom.msgs.hidden = false
    api.dom.list.hidden = false
    await api.suggest({})
    expect(api.dom.msgs.hidden).toBe(true)
    expect(api.dom.list.hidden).toBe(true)
  })

  it('一条草稿都没有时不收消息列表（没东西可让位）', async () => {
    const api = loadReplyPanel(async () => okResponse({ ok: true, replies: [] }))
    api.dom.msgs.hidden = false
    api.dom.list.hidden = false
    await api.suggest({})
    expect(api.dom.msgs.hidden).toBe(false)
    expect(api.dom.list.hidden).toBe(false)
  })

  it('★ engine=rule 时必须说出「这是模板」，不能让用户以为是模型想的', async () => {
    const api = loadReplyPanel(async () => okResponse({
      ok: true,
      engine: 'rule',
      replies: ['收到'],
      note: '本地模型没跑起来：端口被占',
    }))
    await api.suggest({})
    expect(api.dom.note.textContent).toContain('模板建议')
    expect(api.dom.note.textContent).toContain('端口被占')
  })

  it('后端说拿不到时，提示行要红并且说清是「拉不到」', async () => {
    const api = loadReplyPanel(async () => okResponse({ ok: false, error: '读微信会话失败：没开' }))
    await api.suggest({})
    expect(api.dom.note.textContent).toContain('读微信会话失败')
    expect(api.dom.note.className).toBe('err')
    expect(api.dom.replies.innerHTML).toBe('')
  })

  it('网络层失败也要有话说，不能只留一个空块', async () => {
    const api = loadReplyPanel(async () => { throw new Error('HTTP 500') })
    await api.suggest({})
    expect(api.dom.note.textContent).toContain('回复建议拉不到')
    expect(api.dom.note.textContent).toContain('HTTP 500')
    expect(api.dom.note.className).toBe('err')
  })

  it('★ 连点「换一批」：旧响应后到也不能顶掉新草稿', async () => {
    const first = deferred<FakeResponse>()
    const second = deferred<FakeResponse>()
    const queue = [first.promise, second.promise]
    const api = loadReplyPanel(() => queue.shift() as Promise<FakeResponse>)

    const p1 = api.suggest({})
    const p2 = api.suggest({})
    second.resolve(okResponse({ ...OK, replies: ['新的'] }))
    await p2
    expect(api.dom.replies.innerHTML).toContain('新的')

    first.resolve(okResponse({ ...OK, replies: ['旧的'] }))
    await p1
    expect(api.dom.replies.innerHTML).toContain('新的')
    expect(api.dom.replies.innerHTML).not.toContain('旧的')
  })
})

describe('怎么回：草稿同时喂给主面板那一行', () => {
  it('拿到草稿就交给 applyCand（不然主卡永远只会说「读当前会话」）', async () => {
    const api = loadReplyPanel(async () => okResponse(OK))
    await api.suggest({})
    expect(api.applied()).toHaveLength(1)
    expect(api.applied()[0].replies).toEqual(OK.replies)
  })

  it('★ 后端说失败时不交（残次品会在主卡上变成一句假话）', async () => {
    const api = loadReplyPanel(async () => okResponse({ ok: false, error: '读不到微信窗口' }))
    await api.suggest({})
    expect(api.applied()).toHaveLength(0)
  })
})

describe('怎么回：点一下就填进输入框', () => {
  // 抠出真实的事件委托块（注释 → 「换一批」那行之前），在沙箱里跑。
  const a = html.indexOf('// 草稿点一下就填进')
  const b = html.indexOf('if (anaReplyBtnEl)', a)
  const clickBlock = html.slice(a, b)

  function loadClicker() {
    expect(clickBlock.length, '找不到草稿点击委托块').toBeGreaterThan(0)
    const calls = { refresh: 0, layer: 'ana', focus: 0 }
    const sayEl = { value: '', focus: () => { calls.focus++ } }
    let handler: ((e: unknown) => void) | null = null
    const anaRepliesEl = {
      addEventListener: (_type: string, fn: (e: unknown) => void) => { handler = fn },
    }
    new Function(
      'anaRepliesEl', 'sayEl', 'onSayInput', 'setLayer',
      `${clickBlock}\nreturn 1;`,
    )(
      anaRepliesEl,
      sayEl,
      () => { calls.refresh++ },
      (next: string) => { calls.layer = next },
    )
    return { sayEl, calls, click: (e: unknown) => handler?.(e) }
  }

  it('点草稿 → 填值 + 刷新发送按钮 + 退回主面板', () => {
    const { sayEl, calls, click } = loadClicker()
    click({ target: { closest: (sel: string) => (sel === '.rchip' ? { textContent: '哈哈你吃这么快？' } : null) } })
    expect(sayEl.value).toBe('哈哈你吃这么快？')
    expect(calls.refresh).toBe(1)
    expect(calls.layer).toBe('')
    expect(calls.focus).toBe(1)
  })

  it('点在草稿块空白处什么都不做', () => {
    const { sayEl, calls, click } = loadClicker()
    click({ target: { closest: () => null } })
    expect(sayEl.value).toBe('')
    expect(calls.refresh).toBe(0)
    expect(calls.layer).toBe('ana')
  })

  it('「换一批」用的是上一轮的分析结果（含 last_side），且把上一批当 avoid 带回去', () => {
    expect(html).toContain('anaReplyBtnEl.addEventListener("click"')
    // 只传 anaLast、不带 `anaLastReplies.length > 0` 的话，这个按钮等于没做：
    // 同一份提示词再问一次，模型只会把刚才那三条重说一遍。（两个参数缺一不可）
    expect(html).toContain('suggestReplies(anaLast || {}, anaLastReplies.length > 0)')
  })
})

describe('怎么回：接在分析之后', () => {
  it('分析成功自动要草稿；分析失败清空草稿', async () => {
    const urls: string[] = []
    const api = loadReplyPanel(async (url) => {
      urls.push(url)
      return okResponse(OK)
    })
    api.render({
      ok: true,
      chat: '117',
      analysis: { headline: '117' },
      messages: [],
      signals: { last_side: 'left' },
    })
    await new Promise((r) => setTimeout(r, 0))
    expect(urls[0]).toBe('/api/radar/replies?limit=14&screens=0&last_side=left')
    expect(api.dom.replies.innerHTML).toContain('rchip')

    api.render({ ok: false, error: '读不到微信窗口' })
    expect(api.dom.replies.innerHTML).toBe('')
    expect(api.dom.note.textContent).toBe('')
    expect(api.last()).toBeNull()
  })

  it('autoReply=false（预览/量测）不去拉起模型', async () => {
    let calls = 0
    const api = loadReplyPanel(async () => {
      calls++
      return okResponse(OK)
    })
    api.render({ ok: true, chat: '117', analysis: {}, messages: [] }, false)
    await new Promise((r) => setTimeout(r, 0))
    expect(calls).toBe(0)
  })
})
