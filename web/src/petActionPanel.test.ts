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

describe('建议面板：只报「此刻正在发生的事」', () => {
  it('★ 没有任何信号 → 返回空数组，推荐区整块不占高度', () => {
    // 2026-10-27 撤掉了兜底池（点歌 30 / 音效 26 / 分析聊天 24 / 该不该回 22）。
    // 它存在的唯一理由是「空面板会让人以为坏了」—— 现在面板不空：
    // 怎么回是一块整宽主卡（#replyCard），点歌·音效·托管在底部工具行（#toolRow），
    // 变声是状态条上的药丸。于是池子只剩下一个作用：把真正的信号挤到后面去。
    expect(api.rankActions(base())).toEqual([])
  })

  it('★ 空是空数组，不是 undefined —— 调用方一律 rows.length 判空', () => {
    expect(Array.isArray(api.rankActions(base()))).toBe(true)
  })

  it('有信号时只出信号，一个兜底项都不掺', () => {
    const ids = api.rankActions(base({ bgm: true, max: 99 })).map((r) => r.id)
    expect(ids).toEqual(['bgm.toggle'])
  })

  it('★ 搜索专用动作（发语音 / 试听）不进推荐池 —— 主按钮已经承担了它们', () => {
    const ids = api.rankActions(base({ max: 99 })).map((r) => r.id)
    expect(ids).not.toContain('draft.send')
    expect(ids).not.toContain('draft.preview')
  })
})

describe('建议面板：被「一等入口」接管的动作不再进推荐池', () => {
  // 每一个都有一个**常驻**控件在管，推荐位里再出现一次就是重复：
  //   怎么回 / 分析聊天 → #replyCard（整宽主卡）
  //   点歌 / 音效 / 托管 → #toolRow（底部工具行）
  //   开变声            → #pill（状态条药丸）
  // searchOnly 的语义因此从「主按钮已经承担了它们」扩成「已经有一等入口」。
  it.each([
    ['radar.open', '怎么回'],
    ['analyze.open', '分析聊天'],
    ['song.find', '点歌'],
    ['sfx.play', '音效'],
    ['autopilot.open', '托管'],
    ['live.toggle', '开变声'],
  ])('%s（%s）带 searchOnly', (id) => {
    const m = api.ACT_META.find((x) => x.id === id)
    expect(m?.searchOnly).toBe(true)
  })

  it('★ 但它们必须**搜得到** —— 老用户记的是功能名，不是新藏法', () => {
    expect(api.matchActions('歌', base()).map((r) => r.id)).toContain('song.find')
    expect(api.matchActions('音效', base()).map((r) => r.id)).toContain('sfx.play')
    expect(api.matchActions('托管', base()).map((r) => r.id)).toContain('autopilot.open')
    expect(api.matchActions('分析', base()).map((r) => r.id)).toContain('analyze.open')
    // 「该不该回」是 2026-10-24 那版的说法，现在字面上叫「怎么回」，两个词都得能搜到
    expect(api.matchActions('该不该', base()).map((r) => r.id)).toContain('radar.open')
  })

  it('★ searchOnly 不等于绕过能力门 —— 能力关掉照样搜不到', () => {
    const ids = api.matchActions('歌', base({ cap: { ...ALL_CAP, fx: false } })).map((r) => r.id)
    expect(ids).not.toContain('song.find')
  })

  it('★ 使用频率不能把一等入口重新顶回推荐池', () => {
    // bumpFreq 还在（它给真正进池的动作加分），但满分 15 也压不过 searchOnly 那道门
    const ids = api.rankActions(base({ max: 99, freq: { 'sfx.play': 100 } })).map((r) => r.id)
    expect(ids).not.toContain('sfx.play')
    expect(ids).toEqual([])
  })
})

describe('建议面板：上下文信号', () => {
  it('有提案待确认时排第一，且理由是「待确认」', () => {
    const rows = api.rankActions(base({ pending: 1, pendingTitle: '发《晴天》' }))
    expect(rows[0]).toMatchObject({ id: 'proposal.confirm', hint: '待确认' })
    expect(api.labelFor('proposal.confirm', { pendingTitle: '发《晴天》' })).toBe('确认 发《晴天》')
  })

  it('★ 两个信号同时在：提案盖过放歌（已经解析好的事比"正在发生的事"更该处理）', () => {
    const rows = api.rankActions(base({ pending: 1, pendingTitle: 'X', bgm: true }))
    expect(rows[0].id).toBe('proposal.confirm')
    expect(rows[1]).toMatchObject({ id: 'bgm.toggle', hint: '正在放' })
  })

  it('★ 「该回了」不再造推荐行 —— 它现在是主卡上的一个字（#replyCard）', () => {
    // 2026-10-26 用户原话：「这个该不该回怎么没把它变成推荐我怎么回呢？」
    // 那一版的做法是把它做成推荐行、行尾挂第一条草稿。2026-10-27 再进一步：
    // 判断结论（该回了 / 可以回了）写在主卡标题旁，草稿整句写在副行 ——
    // 它不需要跟别的东西挤三个位置，所以不再进 rankActions。
    expect(api.rankActions(base({ radar: 'now' }))).toEqual([])
    expect(api.rankActions(base({ radar: 'soon' }))).toEqual([])
  })

  it('「怎么回」这个词仍然挂在雷达动作上（labelFor 与 kw 都不许丢）', () => {
    expect(api.labelFor('radar.open', {})).toBe('怎么回')
    const meta = api.ACT_META.find((m) => m.id === 'radar.open')!
    expect(meta.kw).toContain('怎么回')
    expect(meta.kw).toContain('该不该')
  })

  it('★ 正在放歌时第一条是「停止放歌」（这时最可能想做的是停）', () => {
    const rows = api.rankActions(base({ bgm: true }))
    expect(rows[0].id).toBe('bgm.toggle')
    expect(api.labelFor('bgm.toggle', { bgm: true })).toBe('停止放歌')
    expect(api.labelFor('bgm.toggle', { bgm: false })).toBe('放歌')
  })

  it('★ 输入框有字时推荐区让给搜索结果（返回空数组，不是 undefined）', () => {
    const rows = api.rankActions(base({ query: '晴天', text: '晴天', bgm: true }))
    expect(rows).toEqual([])
  })

  it('最多返回 max 条', () => {
    const both = { pending: 1, pendingTitle: 'X', bgm: true }
    expect(api.rankActions(base({ ...both, max: 2 }))).toHaveLength(2)
    expect(api.rankActions(base({ ...both, max: 1 }))).toHaveLength(1)
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
