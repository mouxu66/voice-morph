import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 「所有消费后端接口的 UI 面」清点（2026-09-20 补）。
 *
 * 由来：插件化第 4 步把**主应用**（`web/src`）的路由与侧栏改成清单驱动，但没人清点过
 * **还有哪些别的面也在直接调后端**。结果第 6 步之后才发现**桌宠是第二个前端面，从头
 * 到尾没接过能力清单** —— 关掉 `sound.rvc-live` 后桌宠不但按下去 404，还会因为
 * `tick()` 拿那两个接口当离线判据而**永久误报「后端离线」**（见 `petCapabilityGate.test.ts`）。
 *
 * 所以这里钉一把尺子：**受跟踪文件里凡是直接打后端端口的、且不在 `web/src` 下的，
 * 必须逐个登记在下面的白名单里**。以后新加一个面（新的独立 HTML、新的渲染进程……）
 * 会直接把这条测试变红，逼你去想「它需不需要认能力清单」，而不是等用户发现。
 */

const repoRoot = path.resolve(process.cwd(), '..')

/** 直接打后端端口（开发代理 / 生产都是它）的文件。 */
function backendCallers(): string[] {
  const out = execFileSync('git', ['ls-files'], { cwd: repoRoot, encoding: 'utf8' })
  const files = out.split('\n').filter(Boolean)
  const hits: string[] = []
  for (const f of files) {
    // 只看可能是前端/脚本的扩展名；后端自己的 .py 不在这个口径里
    if (!/\.(html|cjs|mjs|js|ts|tsx|ps1)$/.test(f)) continue
    if (f.startsWith('web/src/')) continue // 主应用：已经由清单驱动
    if (f.includes('node_modules/')) continue
    const abs = path.join(repoRoot, f)
    let txt: string
    try {
      txt = readFileSync(abs, 'utf8')
    } catch {
      continue // 读不到（权限 / 已删）就跳过，不让它把整条清点带红
    }
    // 后端端口 8000 的字面量（开发代理 / 局域网直连 / 生产都是它）。
    // 含 IPv4（127.0.0.1 / 192.168.x.x 等）与 localhost 两种写法。
    if (/(?:\d{1,3}\.){3}\d{1,3}:8000|localhost:8000/.test(txt)) hits.push(f)
  }
  return hits.sort()
}

/**
 * 已登记的面。value 说明它为什么**不需要**（或已经）接能力清单。
 *
 * 加新面时请把 id 与理由一起写进来 —— 理由不是走过场：写下「为什么不接」这个动作
 * 本身就是在替未来的自己复查一遍。
 */
const REGISTRY: Record<string, string> = {
  'web/electron/pet/pet.html': '桌宠渲染侧；已接能力清单（`capStateFromCatalog`，见 petCapabilityGate.test.ts）',
  'seed_vc_demo.html':
    '开发用独立演示页，调 /api/seedvc/*（属 sound.offline-vc）；未被主进程/package.json 引用 → **不进包**，故不做门控',
  'web/vite.config.ts': '开发期 dev proxy（把 /api 转发到 8000），不是 UI 面',
  'tools/test-pet-renderer-script.cjs': '桌宠脚本的语法自检工具，不是 UI 面',
  'mobile/src/store.ts':
    '移动端 host 默认值（192.168.1.10:8000，用户可在设置页改）；已接能力清单（mobile/src/capabilities.ts）',
  'mobile/src/api.ts':
    '移动端 API 层；实际调用经 store 的可编辑 host 拼路径（无硬编码调用字面量，注释里的 0.0.0.0:8000 是后端监听说明），已接能力清单',
  'mobile/app/settings.tsx':
    '移动端设置页；placeholder/提示里出现局域网地址形如 http://192.168.1.10:8000，非调用字面量',
}

describe('所有调后端的 UI 面都已登记（新增面必须显式加进来）', () => {
  it('cwd 确实是 web 目录（否则下面会扫到错的范围）', () => {
    const pkg = JSON.parse(readFileSync(path.join(process.cwd(), 'package.json'), 'utf8')) as { name: string }
    expect(pkg.name).toBe('voice-morph-desktop')
  })

  it('扫描确实扫到了东西（防正则失效后空集合假绿）', () => {
    expect(backendCallers().length).toBeGreaterThan(0)
  })

  it('每个面都在登记表里', () => {
    const unknown = backendCallers().filter((f) => !(f in REGISTRY))
    expect(
      unknown,
      `这些文件直接打了后端端口但没登记。新增 UI 面时必须想清楚「它要不要认能力清单」，` +
        `然后把 id 与理由写进 backendSurfaceAudit.test.ts 的 REGISTRY：\n  ${unknown.join('\n  ')}`,
    ).toEqual([])
  })

  it('登记表里没有已经不存在的文件（删了就要同步删，别留死条目）', () => {
    const present = new Set(backendCallers())
    const stale = Object.keys(REGISTRY).filter((f) => !present.has(f))
    expect(stale, `登记了但已经不打后端了：${stale.join(', ')}`).toEqual([])
  })

  it('★ 桌宠这个面必须真的接了能力清单（不是只登记一下）', () => {
    const pet = readFileSync(path.join(repoRoot, 'web/electron/pet/pet.html'), 'utf8')
    // 只登记、没接上 = 白登记。这里复钉一次最关键的证据。
    expect(pet).toContain('===CAP-GATE-START===')
    expect(pet).toContain('if (CAP.live) {')
  })
})
