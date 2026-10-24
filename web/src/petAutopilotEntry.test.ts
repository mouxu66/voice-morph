import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 桌宠建议区第 4 条固定入口「托管」的**接线**（2026-10-24）。
 *
 * 背景：用户装好新版后报「重启之后仍然没有做按钮」。根因不是没同步，而是极简模式下
 * 侧栏把「桌面」组折叠成一行，「全局托管」在导航里根本看不见。拍板方案是在桌宠面板
 * 加一颗常驻的「托管」胶囊 —— 它必须能真的把主窗口切到 /autopilot 页。
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

describe('桌宠「托管」入口：渲染层', () => {
  it('输入框为空时固定追加 autopilot.open（不靠推荐排序抢位置）', () => {
    expect(refreshRecs).toContain('id: "autopilot.open", label: labelFor("autopilot.open", ctx)')
    // 推荐区 4 条（3 条随上下文 + 固定入口）；搜索区仍是 3 条
    expect(refreshRecs).toContain('rows.slice(0, ctx.query ? 3 : 4)')
  })

  it('★ 微信能力关掉时不追加（后端没挂路由，点了就是 404）', () => {
    expect(refreshRecs).toMatch(/if \(ctx\.cap\.wechat !== false\) \{[\s\S]*?autopilot\.open/)
  })

  it('点它走 preload 的 openPage，且浏览器里单开 pet.html 时不会抛错', () => {
    expect(html).toContain('window.pet.openPage("/autopilot")')
    expect(html).toMatch(/if \(window\.pet && window\.pet\.openPage\)/)
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
