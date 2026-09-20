import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 为什么需要这一组测试（2026-09-20 修 `npm run lint` 时补）。
 *
 * `npm run lint` 此前长期是坏的：`eslint.config.mjs` 里 import 了 `@eslint/js` 与
 * `typescript-eslint`，而 package.json **两个都没声明** —— 本机之所以 import 得到，
 * 纯粹因为它们被别的包**传递依赖**带进了 node_modules。任何一台新机器 `npm ci`
 * 之后 lint 直接 `ERR_MODULE_NOT_FOUND`，且报错看不出跟 package.json 有关。
 *
 * 这类洞不会让别的测试变红（没人 import eslint 配置），只会在某台新机器上变成一句
 * 看不懂的报错 —— 所以它需要一个专门的守卫，而不是指望人记得。
 */

const webRoot = process.cwd()

type Pkg = {
  name: string
  dependencies?: Record<string, string>
  devDependencies?: Record<string, string>
}

const pkg = JSON.parse(readFileSync(path.join(webRoot, 'package.json'), 'utf8')) as Pkg
const declared = new Set([
  ...Object.keys(pkg.dependencies ?? {}),
  ...Object.keys(pkg.devDependencies ?? {}),
])

function read(rel: string): string {
  return readFileSync(path.join(webRoot, rel), 'utf8')
}

/**
 * 去掉注释再断言。
 * 坑（本次实测踩到）：`eslint.config.mjs` 的注释里**就在解释**为什么不用
 * `eslint-plugin-prettier`，直接 `toContain` 会命中注释本身。
 */
function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

/** 取一个 ES module 文件里 `import ... from "x"` / `import "x"` 的裸包名。 */
function importedSpecifiers(src: string): string[] {
  const out: string[] = []
  const fromRe = /^\s*import\s+[^'"]*?\bfrom\s+['"]([^'"]+)['"]/gm
  const bareRe = /^\s*import\s+['"]([^'"]+)['"]/gm
  for (const re of [fromRe, bareRe]) {
    let m: RegExpExecArray | null
    while ((m = re.exec(src)) !== null) out.push(m[1])
  }
  // 相对路径 / 绝对路径 / node: 内置模块不算第三方包
  return out.filter((s) => !s.startsWith('.') && !s.startsWith('/') && !s.startsWith('node:'))
}

describe('eslint 配置的依赖必须真的声明在 package.json 里', () => {
  // 先钉住 cwd：万一将来 vitest 从别处启动，下面的断言会读到错误的 package.json
  // 而**照旧全绿**（最危险的失败方式），所以这条必须先红。
  it('cwd 确实是 web 根目录', () => {
    expect(pkg.name).toBe('voice-morph-desktop')
  })

  it('eslint.config.mjs import 的每个包都有声明', () => {
    const bare = importedSpecifiers(read('eslint.config.mjs'))
    // 正则写错会退化成空数组 → 静默假绿。先保证它真的抓到了东西。
    expect(bare.length).toBeGreaterThan(0)

    const missing = bare.filter((s) => !declared.has(s))
    expect(
      missing,
      `这些包被 eslint.config.mjs import，但 package.json 里没声明（本机靠传递依赖侥幸能跑，新机器 npm ci 就炸）：${missing.join(', ')}`,
    ).toEqual([])
  })

  it('排版不再塞进 lint：不用 eslint-plugin-prettier / prettier/prettier', () => {
    // 2026-09-20 实测：开着 `prettier/prettier: error` 时 lint 报 3373 个错误，
    // 全是 `Delete ';'` 之类，**没有一个是真缺陷**（仓库从来没被 prettier 格式化过）；
    // 关掉后只剩 9 个真问题。排版交给 `npm run format`，别再搬回 lint。
    const src = read('eslint.config.mjs')
    expect(importedSpecifiers(src)).not.toContain('eslint-plugin-prettier')
    expect(stripComments(src)).not.toContain('prettier/prettier')
  })

  it('.prettierrc 与仓库实际风格一致：双引号 + 无分号', () => {
    // 实测（2026-09-20）：19678 行里只有 1476 行以 `;` 结尾，且其中 1324 行是缩进的
    // 接口/类型成员（顶格语句带分号的仅 152 行）；415 个 import **全部**是双引号。
    // 旧配置写的是 `semi: true` + `singleQuote: true`，两样都跟仓库相反。
    const cfg = JSON.parse(read('.prettierrc')) as { semi: boolean; singleQuote: boolean }
    expect(cfg.semi).toBe(false)
    expect(cfg.singleQuote).toBe(false)
  })
})
