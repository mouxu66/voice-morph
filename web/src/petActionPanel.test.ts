import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠「建议面板」的排序引擎（`web/electron/pet/pet.html`，2026-10-23 二次重构）。
 *
 * 这一版的全部风险都集中在 `rankActions()` 一处：面板上只有 3 个位置，
 * 排错一条用户就看不到该做的事；兜底池漏一条，空面板就像坏了一样。
 * 而这种「排序 + 打分」的代码，用 grep 断言字符串是**测不出**错的——
 * 上一轮 `radarSigRows` 把 null 写成 `0s` 就是这么混过去的。
 *
 * 所以这里把真实函数从 pet.html 里抽出来跑（与 petCapabilityGate / petRadarPanel
 * 同一套沙箱做法）：HTML 里改坏了，这里必须红。
 */

const html = readFileSync(path.join(process.cwd(), 'electron', 'pet', 'pet.html'), 'utf8')

/**
 * 按括号配平从 HTML 里切一段源码，跳过字符串字面量（'…' / "…" / `…`）。
 *
 * 为什么不能只数 `{` 和 `}`：`ACT_META` 的 kw 里有中文和空格，一旦有人在里面
 * 写了带花括号的短语（例如 `{歌}`），简单计数就会切错位置，而错位之后的报错
 * 会指向一句完全无关的代码。
 */
function sliceBalanced(src: string, marker: string): string {
  const start = src.indexOf(marker)
  expect(start, `pet.html 里找不到 ${marker}`).toBeGreaterThan(-1)
  const openIdx = src.indexOf('{', start)
  const arrIdx = src.indexOf('[', start)
  // 数组字面量会先遇到 `[`，对象会先遇到 `{`
  const isArray = arrIdx > -1 && (openIdx === -1 || arrIdx < openIdx)
  const open = isArray ? '[' : '{'
  const close = isArray ? ']' : '}'
  let depth = 0
  let quote = ''
  for (let i = isArray ? arrIdx : openIdx; i < src.length; i++) {
    const ch = src[i]
    if (quote) {
      if (ch === '\\') i++
      else if (ch === quote) quote = ''
      continue
    }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue }
    if (ch === open) depth++
    else if (ch === close && --depth === 0) return src.slice(start, i + 1)
  }
  throw new Error(`${marker} 括号不配平`)
}

const SOURCE = [
  sliceBalanced(html, 'const ACT_META = ['),
  sliceBalanced(html, 'const ACT_RUN = {'),
  sliceBalanced(html, 'function actAvailable('),
  sliceBalanced(html, 'function labelFor('),
  sliceBalanced(html, 'function rankActions('),
  sliceBalanced(html, 'function matchActions('),
].join('\n')

type Row = { id: string; score: number; hint: string }
type Ctx = Record<string, unknown>

const api = new Function(
  `${SOURCE}
  return { ACT_META, ACT_RUN, actAvailable, labelFor, rankActions, matchActions };`,
)() as {
  ACT_META: Array<{ id: string; label: string; hint: string; cap: string; kw: string; searchOnly?: boolean }>
  ACT_RUN: Record<string, unknown>
  actAvailable: (m: unknown, c: unknown) => boolean
  labelFor: (id: string, c: Ctx) => string
  rankActions: (c: Ctx) => Row[]
  matchActions: (q: string, c: Ctx) => Row[]
}

const ALL_CAP = { fx: true, radar: true, live: true, wechat: true }
const base = (over: Ctx = {}): Ctx => ({
  draft: 'idle', text: '', query: '', radar: '', bgm: false, live: false,
  pending: 0, pendingTitle: '', cap: { ...ALL_CAP }, freq: {}, max: 3, ...over,
})

describe('建议面板：空态必须给得出来', () => {
  it('什么都没有时也要有三条兜底（空面板会让人以为坏了）', () => {
    const rows = api.rankActions(base())
    expect(rows.map((r) => r.id)).toEqual(['song.find', 'sfx.play', 'analyze.open'])
  })

  it('★ 兜底里的「分析聊天」是用户 2026-10-23 点名要的那个按钮', () => {
    // 原话：「有个按钮能自动分析，就是点它就能自动分析。」
    // 它必须在空面板的前三条里，而且打字搜「分析」也要搜得到 ——
    // 否则用户只能靠猜「这个功能藏在哪」。
    expect(api.labelFor('analyze.open', {})).toBe('分析聊天')
    expect(api.matchActions('分析', base()).map((r) => r.id)).toContain('analyze.open')
  })

  it('★ 能力关掉的动作不能出现在兜底里（点了就是 404）', () => {
    const rows = api.rankActions(base({ cap: { ...ALL_CAP, radar: false, fx: false } }))
    const ids = rows.map((r) => r.id)
    expect(ids).not.toContain('radar.open')
    expect(ids).not.toContain('analyze.open')
    expect(ids).not.toContain('song.find')
    // 还能剩什么就显示什么，但不能因此变成空数组
    expect(ids.length).toBeGreaterThan(0)
  })

  it('★ 搜索专用动作（发语音 / 试听）不进推荐池 —— 主按钮已经承担了它们', () => {
    const ids = api.rankActions(base({ max: 99 })).map((r) => r.id)
    expect(ids).not.toContain('draft.send')
    expect(ids).not.toContain('draft.preview')
  })
})

describe('建议面板：上下文信号', () => {
  it('有提案待确认时排第一，且理由是「待确认」', () => {
    const rows = api.rankActions(base({ pending: 1, pendingTitle: '发《晴天》' }))
    expect(rows[0]).toMatchObject({ id: 'proposal.confirm', hint: '待确认' })
    expect(api.labelFor('proposal.confirm', { pendingTitle: '发《晴天》' })).toBe('确认 发《晴天》')
  })

  it('★ 两个信号同时在：提案盖过雷达（已经解析好的事比「该回了」更具体）', () => {
    const rows = api.rankActions(base({ pending: 1, pendingTitle: 'X', radar: 'now' }))
    expect(rows[0].id).toBe('proposal.confirm')
    expect(rows[1]).toMatchObject({ id: 'radar.open', hint: '对方在等' })
  })

  it('雷达 now / soon 的理由不同（now 才值得打断）', () => {
    expect(api.rankActions(base({ radar: 'now' }))[0]).toMatchObject({ id: 'radar.open', hint: '对方在等' })
    const soon = api.rankActions(base({ radar: 'soon' }))
    expect(soon[0]).toMatchObject({ id: 'radar.open', hint: '可以回了' })
    expect(soon[0].score).toBeLessThan(api.rankActions(base({ radar: 'now' }))[0].score)
  })

  it('★ 正在放歌时第一条是「停止放歌」（这时最可能想做的是停）', () => {
    const rows = api.rankActions(base({ bgm: true }))
    expect(rows[0].id).toBe('bgm.toggle')
    expect(api.labelFor('bgm.toggle', { bgm: true })).toBe('停止放歌')
    expect(api.labelFor('bgm.toggle', { bgm: false })).toBe('放歌')
  })

  it('没开变声时「开变声」在池子里，但排在兜底三项之后', () => {
    const ids = api.rankActions(base({ max: 99 })).map((r) => r.id)
    expect(ids).toContain('live.toggle')
    expect(ids.indexOf('live.toggle')).toBeGreaterThan(ids.indexOf('radar.open'))
  })

  it('★ 输入框有字时推荐区让给搜索结果（返回空数组，不是 undefined）', () => {
    const rows = api.rankActions(base({ query: '晴天', text: '晴天' }))
    expect(rows).toEqual([])
  })

  it('★ 使用频率能把「开变声」顶进前三条（这就是「不用每次打字」的那一半）', () => {
    const cold = api.rankActions(base()).map((r) => r.id)
    expect(cold).not.toContain('live.toggle')
    // 18 底分 + 5 次 × 3 = 33 > sfx.play 的 26 / analyze.open 的 24
    const warm = api.rankActions(base({ freq: { 'live.toggle': 5 } })).map((r) => r.id)
    expect(warm).toContain('live.toggle')
  })

  it('频率加成封顶 15（用 100 次也不会盖过「该回了」）', () => {
    const rows = api.rankActions(base({ radar: 'now', freq: { 'sfx.play': 100 } }))
    expect(rows[0].id).toBe('radar.open')
  })

  it('最多返回 max 条', () => {
    expect(api.rankActions(base({ max: 2 }))).toHaveLength(2)
    expect(api.rankActions(base({ max: 1 }))).toHaveLength(1)
  })
})

describe('建议面板：输入框搜索', () => {
  it('打「歌」能搜到点歌，打「变声」能搜到变声开关', () => {
    expect(api.matchActions('歌', base()).map((r) => r.id)).toContain('song.find')
    expect(api.matchActions('变声', base()).map((r) => r.id)).toContain('live.toggle')
  })

  it('★ 搜索专用动作在搜索里**必须**出现（否则用户想发这段却搜不到）', () => {
    const ids = api.matchActions('发', base({ text: '今天真开心' })).map((r) => r.id)
    expect(ids).toContain('draft.send')
  })

  it('英文关键词也能搜（kw 是双语）', () => {
    expect(api.matchActions('music', base()).map((r) => r.id)).toContain('song.find')
  })

  it('能力关掉的动作不参与匹配', () => {
    const ids = api.matchActions('歌', base({ cap: { ...ALL_CAP, fx: false } })).map((r) => r.id)
    expect(ids).not.toContain('song.find')
  })

  it('空串不返回任何结果（否则面板一浮现就全是候选）', () => {
    expect(api.matchActions('', base())).toEqual([])
    expect(api.matchActions('   ', base())).toEqual([])
  })

  it('★ 搜不到就返回空数组 —— 不能拿兜底项充数（那会让用户以为搜到了）', () => {
    expect(api.matchActions('zzzz不存在的东西', base())).toEqual([])
  })
})

describe('建议面板：注册表完整性', () => {
  it('★ 每个 ACT_META 都有 run，每个 run 都有 meta（漏一个就是死按钮）', () => {
    const metaIds = api.ACT_META.map((m) => m.id).sort()
    const runIds = Object.keys(api.ACT_RUN).filter((k) => k !== 'proposal.confirm').sort()
    expect(runIds).toEqual(metaIds)
  })

  it('meta 的 id 不重复', () => {
    const ids = api.ACT_META.map((m) => m.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('labelFor 对未知 id 不抛错、也不编名字', () => {
    expect(api.labelFor('nope', {})).toBe('nope')
  })
})

describe('建议面板：「托管」是固定入口，不参与推荐排序', () => {
  it('★ 它是 wechat 能力下的动作，关掉微信能力就不该出现', () => {
    const m = api.ACT_META.find((x) => x.id === 'autopilot.open')
    expect(m).toMatchObject({ label: '托管', cap: 'wechat' })
    expect(api.matchActions('托管', base()).map((r) => r.id)).toContain('autopilot.open')
    expect(
      api.matchActions('托管', base({ cap: { ...ALL_CAP, wechat: false } })).map((r) => r.id),
    ).not.toContain('autopilot.open')
  })

  it('★ 它**不在** rankActions 的池子里 —— 池子只有 3 格，被信号挤掉就等于找不到', () => {
    // 推荐池按「此刻该做什么」排序；托管是「去哪开启」的固定页，必须永远占第 4 条
    // （由 refreshRecs 追加，见 petAutopilotEntry.test.ts）。进了池子就会在
    // 「有提案 / 该回了 / 正在放歌」时消失 —— 这正是 2026-10-24 报障的形态。
    const ids = api.rankActions(base({ max: 99 })).map((r) => r.id)
    expect(ids).not.toContain('autopilot.open')
  })
})
