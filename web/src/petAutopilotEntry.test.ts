import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠「托管」入口的**接线**（2026-10-24 立，2026-10-27 改位置）。
 *
 * 背景：用户装好新版后报「重启之后仍然没有做按钮」。根因不是没同步，而是极简模式下
 * 侧栏把「桌面」组折叠成一行，「全局托管」在导航里根本看不见。拍板方案是在桌宠面板
 * 加一颗常驻的「托管」入口 —— 它必须能真的把主窗口切到 /autopilot 页。
 *
 * 2026-10-27 那颗入口从「建议区固定第 4 条 chip」搬到**底部工具行**（#toolRow）。
 * 「位置常驻」这条规矩没变，只是换了实现：现在它写着自己的名字，而不是一格要靠猜的
 * chip。下面钉的是新契约 —— 搬走了但要保证「还在、还连着、能力关掉会收」。
 *
 * 这条链跨三个文件（pet.html → preload.cjs → main.cjs），任何一环漏掉都是「点了没反应」
 * 或「按钮根本不出现」，而这类缺失用 grep 单看一个文件发现不了。所以这里逐环钉住。
 */

const webRoot = process.cwd()
const html = readFileSync(path.join(webRoot, 'electron', 'pet', 'pet.html'), 'utf8')
const preload = readFileSync(path.join(webRoot, 'electron', 'pet', 'preload.cjs'), 'utf8')
const main = readFileSync(path.join(webRoot, 'electron', 'main.cjs'), 'utf8')

/** refreshRecs 的函数体：从它开始到下一个顶层函数 runAction 之前。 */
const refreshRecs = html.slice(html.indexOf('function refreshRecs('), html.indexOf('function runAction('))

/** 按花括号配平抠出一个顶层函数（与 petReplySuggest.test.ts 同一套做法）。 */
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

describe('桌宠「托管」入口：渲染层', () => {
  it('入口在工具行里写着自己的名字，不再靠推荐排序抢位置', () => {
    // ① 工具行里有一颗真的按钮，带 data-tool（点它走 runAction）
    expect(html).toMatch(/<div id="toolBox"[\s\S]{0,600}data-tool="autopilot\.open"/)
    expect(html).toMatch(/data-tool="autopilot\.open"[\s\S]{0,300}>托管<\/button>/)
    // ② 推荐区不再追加第 4 条 —— 那块位置还给内容
    expect(refreshRecs).not.toContain('rows.slice(0, ctx.query ? 3 : 4)')
    expect(refreshRecs).toContain('rows.slice(0, 3)')
    // ③ 它带 searchOnly，所以频率再高也顶不回推荐池（搜索里仍找得到）
    expect(html).toMatch(/id: "autopilot\.open"[\s\S]{0,240}searchOnly: true/)
  })

  it('点它走 preload 的 openPage，且浏览器里单开 pet.html 时不会抛错', () => {
    expect(html).toContain('window.pet.openPage("/autopilot")')
    expect(html).toMatch(/if \(window\.pet && window\.pet\.openPage\)/)
  })
})

describe('桌宠「托管」入口：能力关掉就收掉', () => {
  /** 工具行里的三颗按钮（托管那颗要能被单独收起）。 */
  function fakeToolBox() {
    const btns: Record<string, { hidden: boolean }> = {
      'song.find': { hidden: false },
      'sfx.play': { hidden: false },
      'autopilot.open': { hidden: false },
    }
    return {
      btns,
      box: {
        querySelector: (sel: string) => {
          const m = /data-tool="([^"]+)"/.exec(sel)
          return m && btns[m[1]] ? btns[m[1]] : null
        },
      },
    }
  }

  function run(cap: Record<string, unknown>) {
    const t = fakeToolBox()
    const fn = new Function(
      'dom',
      `const toolBoxEl = dom.box;\n${extractFn('syncToolCaps')}\nreturn syncToolCaps;`,
    )(t) as (c: unknown) => void
    fn(cap)
    return t.btns
  }

  it('★ 微信能力关掉时把「托管」收掉（那一页没有后端路由，点了就是 404）', () => {
    const b = run({ wechat: false })
    expect(b['autopilot.open'].hidden).toBe(true)
    // 另外两颗只看本机后端，不受这条能力位管
    expect(b['song.find'].hidden).toBe(false)
    expect(b['sfx.play'].hidden).toBe(false)
  })

  it('能力在 / 字段缺失时都留着（缺字段 ≠ 关掉，别把按钮误收）', () => {
    expect(run({ wechat: true })['autopilot.open'].hidden).toBe(false)
    expect(run({})['autopilot.open'].hidden).toBe(false)
    expect(run({ radar: false })['autopilot.open'].hidden).toBe(false)
  })

  it('refreshRecs 每次重画都会带上当前能力（不然从别的会话切回来还是旧的）', () => {
    expect(refreshRecs).toContain('syncToolCaps(ctx.cap)')
  })
})

describe('桌宠「托管」入口：IPC 链路', () => {
  it('preload 暴露 openPage → pet:open-page', () => {
    expect(preload).toContain('openPage: (p) => ipcRenderer.send("pet:open-page"')
  })

  it('★ 主进程处理 pet:open-page：只放行站内路径（它会被拼进 executeJavaScript）', () => {
    expect(main).toMatch(/ipcMain\.on\("pet:open-page"/)
    expect(main).toContain('if (!/^\\/[A-Za-z0-9/_-]*$/.test(p)) return;')
    expect(main).toContain('location.hash = ')
  })
})
