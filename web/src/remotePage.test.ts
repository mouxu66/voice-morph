import { readFileSync, existsSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * 手机遥控页（`public/remote.html`）的契约测试。
 *
 * 为什么单独钉这个页面：它是**第三个前端面**（主应用 / 桌宠 / 遥控页），
 * 而它没有任何构建期检查 —— 一个自包含 HTML，写错一个接口路径不会让 tsc 红、
 * 不会让打包失败，只会让用户在手机上点一下 404，且**完全看不出原因**
 * （那正是 `test_audit_endpoint_ownership.py` 模块注释里说过的同一种痛）。
 *
 * 这里钉四条**会静默腐烂**的契约：
 *   1. 页面确实在 `public/` 下 —— 放错目录（比如放 `src/`）Vite 就不会拷进 dist，
 *      于是"本机能看到、手机上 404"；
 *   2. 页面里出现的每个 `/api/...` 路径都**真有对应路由**（后端改名就红）；
 *   3. 一切请求走**同源相对路径**（不写 `:8000`）—— 既避免在
 *      `backendSurfaceAudit.test.ts` 里多一条要维护的登记，也不会因换 IP/换端口失效；
 *   4. token 走 `api_key` 查询参数、且认能力清单 —— 前者是 LAN 鉴权的唯一可行方式
 *      （`<audio src>` 带不了 Header），后者是"关掉的能力不该被裸渲染"的既有约定。
 */

const WEB = process.cwd()
const REPO = path.resolve(WEB, '..')
const M2 = path.join(REPO, 'm2_server')
const PAGE = path.join(WEB, 'public', 'remote.html')

/** 页面里被允许"只写前缀"的路径 —— 必须写明理由，且前缀本身也要能兑现。 */
const DYNAMIC_PREFIXES: Record<string, string> = {
  '/api/media/outputs/':
    '媒体 URL 由 media_api 的 `/api/media/{kind}/{name}` 提供，页面只拼 `.session/` 这一段子路径',
}

/** 后端全量路由（`prefix + path`），从源码扫出来 —— 不维护第二份手写清单。 */
function backendRoutes(): Map<string, string> {
  const apiPrefix =
    /^API_PREFIX\s*=\s*"([^"]+)"/m.exec(readFileSync(path.join(M2, 'runtime.py'), 'utf8'))?.[1] ?? ''

  const routes = new Map<string, string>()
  for (const f of readdirSync(M2)) {
    if (!f.endsWith('.py')) continue
    const txt = readFileSync(path.join(M2, f), 'utf8')
    const m = /APIRouter\(\s*prefix\s*=\s*(?:"([^"]*)"|API_PREFIX)/.exec(txt)
    if (!m) continue
    const prefix = m[1] ?? apiPrefix
    for (const r of txt.matchAll(/@router\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"/g)) {
      routes.set(prefix + r[1], f)
    }
  }
  return routes
}

/** 把声明的路由转成匹配用正则：`/api/media/{kind}/{name}` → `^/api/media/[^/]+/[^/]+$`。 */
function routeToRx(declared: string): RegExp {
  const body = declared
    .replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    .replace(/\\\{[^}]+\\\}/g, '[^/]+')
  return new RegExp('^' + body + '$')
}

/** 页面里出现的 API 路径字面量（含 `?query` 的截断掉）。 */
function pageApiPaths(): string[] {
  const txt = readFileSync(PAGE, 'utf8')
  const out = new Set<string>()
  for (const m of txt.matchAll(/["'`](\/api\/[^"'`\s]*)["'`]/g)) out.add(m[1].split('?')[0])
  return [...out].sort()
}

describe('手机遥控页 remote.html', () => {
  it('放在 public/ 下（否则不会进 dist，手机上直接 404）', () => {
    expect(existsSync(PAGE), `缺文件：${PAGE}`).toBe(true)
    expect(readFileSync(PAGE, 'utf8').length).toBeGreaterThan(2000)
  })

  it('★ 页面调的每个接口都真有对应路由', () => {
    const routes = backendRoutes()
    expect(routes.size, '后端路由一条都没扫到 —— 抽取正则失效了，别让它假绿').toBeGreaterThan(50)

    const dyn = Object.keys(DYNAMIC_PREFIXES)
    const missing = pageApiPaths().filter((p) => {
      if (dyn.some((k) => p.startsWith(k))) return false
      if (routes.has(p)) return false
      // 带占位符的路径（`/api/xxx/{id}`）按通配比对
      for (const declared of routes.keys()) {
        if (!declared.includes('{')) continue
        if (routeToRx(declared).test(p)) return false
      }
      return true
    })
    expect(
      missing,
      `页面调了后端不存在的接口（改名/删除后忘了同步页面）：\n  ${missing.join('\n  ')}`,
    ).toEqual([])
  })

  it('允许"只写前缀"的条目必须既有效又不失效（防死条目）', () => {
    const routes = backendRoutes()
    for (const [prefix, why] of Object.entries(DYNAMIC_PREFIXES)) {
      expect(why.length, `${prefix} 没写理由`).toBeGreaterThan(8)
      expect(
        pageApiPaths().some((p) => p.startsWith(prefix)),
        `登记了 ${prefix} 但页面已经不用它了`,
      ).toBe(true)
      // 前缀要能真的兑现：某个声明路由（字面前缀或带 `{占位符}`）能覆盖它
      const probe = prefix + 'probe'
      const owner = [...routes.keys()].find(
        (r) => r.startsWith(prefix) || routeToRx(r).test(probe),
      )
      expect(owner, `${prefix} 在后端已经不存在了`).toBeTruthy()
    }
  })

  it('一切请求同源相对路径（不写后端端口字面量）', () => {
    const txt = readFileSync(PAGE, 'utf8')
    const hits = txt.match(/(?:\d{1,3}\.){3}\d{1,3}:\d+|localhost:\d+|https?:\/\/[^\s"'`]+/g) ?? []
    // 注释里说明用法时写法示例是允许的，但**不能出现在代码里**：下面按"是否带引号赋值"粗筛，
    // 只要不是 fetch/src 目标就不算。真正要防的是"把地址写死"。
    const inCode = hits.filter((h) => new RegExp(`(fetch|src|href)\\s*[=(]\\s*["'\`]?[^"'\`]*${h.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`).test(txt))
    expect(inCode, `遥控页不该把后端地址写死（换 IP 就失效）：${inCode.join(', ')}`).toEqual([])
  })

  it('token 走 api_key 查询参数（原生播放器带不了 Header）', () => {
    const txt = readFileSync(PAGE, 'utf8')
    expect(txt).toContain('api_key')
    expect(txt, 'auth 不能只认 Header —— 手机上的 <audio src> 加不了自定义头').toMatch(/withKey|api_key=/)
  })

  it('响应式：声明 viewport 与刘海屏安全区', () => {
    const txt = readFileSync(PAGE, 'utf8')
    expect(txt).toMatch(/<meta[^>]+name="viewport"[^>]+width=device-width/)
    expect(txt).toContain('env(safe-area-inset-bottom)')
  })

  it('认能力清单（关掉的能力不能被裸渲染成可点按钮）', () => {
    const txt = readFileSync(PAGE, 'utf8')
    expect(txt, '第三个前端面必须与主应用/桌宠用同一份清单').toContain('/api/plugins')
    expect(txt).toMatch(/capOff\(/)
  })

  it('★ 认 block_reason：两条发送路径都要提前拦住「注定失败」', () => {
    // 后端 /api/wechat/precheck 的 block_reason 非空 = 这次发送**注定失败**
    // （判据复用 send_text 的 _send_preflight，页面不在前端重写一份）。
    //
    // 为什么值得钉：手机这条路要付出「录 0..59 秒 → 换声十几秒」才轮到发送，
    // 而「微信没开 / 被收进托盘」是**按下之前**就知道的事。桌宠早就这么做了
    // （pet.html 的 renderWechatPrecheck），遥控页漏掉的话，同一个用户会在两处
    // 得到完全不同的待遇：电脑上立刻被拦住，手机上白等十几秒才失败。
    //
    // 页面里有**两条**发送路径（语音发送 tab / 手机当麦 tab），各有一条预检 ——
    // 只改一处等于只拦住一条。所以按函数体分别查，而不是整页查一次。
    const html = readFileSync(PAGE, 'utf8')
    for (const fn of ['refreshPrecheck', 'refreshMicPrecheck']) {
      const at = html.indexOf(`function ${fn}`)
      expect(at, `找不到 ${fn} —— 函数被改名了？`).toBeGreaterThan(0)
      expect(
        html.slice(at, at + 1000),
        `${fn} 没认 block_reason —— 这条路径会在白等十几秒后才报错`,
      ).toContain('block_reason')
    }
  })
})

/** 后端 .py 多数是 CRLF —— 解析前统一成 LF，别让行尾把正则弄红。 */
function readPy(name: string): string {
  return readFileSync(path.join(M2, name), 'utf8').replace(/\r\n/g, '\n')
}

/** 从 `capture_api.py` 的 `/capture/mic/upload` 里读出 Form 参数名（不维护第二份清单）。 */
function backendUploadFormFields(): string[] {
  const txt = readPy('capture_api.py')
  // 找装饰器本身（不要找文档字符串里那处同名字样，否则找错了地方还以为对）
  const at = txt.indexOf('@router.post("/capture/mic/upload")')
  expect(at, 'capture_api.py 里找不到上传端点 —— 抽取失效了，别让它假绿').toBeGreaterThan(-1)
  const block = txt.slice(at, at + 1500)
  return [...block.matchAll(/(\w+)\s*:\s*[\w\[\]| ]+\s*=\s*Form\(/g)].map((m) => m[1]).sort()
}

/** `SendVoiceReq` 的字段名（发送语音那一步的实际契约）。 */
function backendSendVoiceFields(): string[] {
  const txt = readPy('wechat_voice.py')
  const m = /class SendVoiceReq\(BaseModel\):\n([\s\S]*?)\n\n/.exec(txt)
  expect(m, 'wechat_voice.py 里找不到 SendVoiceReq').toBeTruthy()
  return [...m![1].matchAll(/^\s{4}(\w+)\s*:/gm)].map((x) => x[1])
}

/**
 * 「手机当麦克风」这一段的契约。
 *
 * 它与上面那组同源，但多一层**跨文件字段名**的耦合：页面用 FormData 拼的字段名、
 * 后端 `Form(...)` 的参数名、`SendVoiceReq` 的字段名，任何一处改名都不会让 tsc 红、
 * 不会让打包失败 —— 只会在手机上弹一句 422 / 后端报“缺 wav”的英文。
 * 所以这里从**后端源码**里读字段名来比，而不是再手写一份。
 */
describe('手机当麦克风（remote.html）', () => {
  const txt = readFileSync(PAGE, 'utf8')

  it('上传的 FormData 字段名与后端 Form(...) 对得上', () => {
    const backend = backendUploadFormFields()
    expect(backend.length, '一个 Form 字段都没抽到').toBeGreaterThan(2)
    expect(backend).toContain('voice_id')

    const m = /const micForm = \(\) => \[([\s\S]*?)\]/.exec(txt)
    expect(m, '页面里的 micForm 没找到 —— 上传参数大概率换了写法').toBeTruthy()
    const page = [...m![1].matchAll(/\['(\w+)'/g)].map((x) => x[1])
    expect(page.length).toBeGreaterThan(0)
    expect(
      page.filter((k) => !backend.includes(k)),
      '页面传了后端不认的字段（拼错名字不会报错，只会静默用默认值）',
    ).toEqual([])
  })

  it('★ 发微信那一步用的字段名与 SendVoiceReq 一致', () => {
    const fields = backendSendVoiceFields()
    expect(fields, 'SendVoiceReq 的字段没抽到').toContain('wav')
    // 页面必须真的把换声后的文件名放进 `wav`（桌面端/桌宠走的是同一个字段）
    expect(txt).toMatch(/send_voice',\s*\{\s*method:\s*'POST',\s*body:\s*\{\s*wav:/)
  })

  it('上传不手写 Content-Type（multipart 的 boundary 必须由浏览器生成）', () => {
    const m = /async function apiForm\(([\s\S]*?)\n {6}\}/.exec(txt)
    expect(m, 'apiForm 没找到').toBeTruthy()
    expect(m![1], '手写 Content-Type 会把 boundary 写坏，后端解不出表单').not.toContain(
      'Content-Type',
    )
    expect(m![1]).toMatch(/method: 'POST'/)
  })

  it('★ 时长上限跟后端同一个数（不是页面里另写的一个魔数）', () => {
    const backend = Number(/^MAX_HOLD_SECONDS\s*=\s*([\d.]+)/m.exec(readPy('mic_capture.py'))?.[1])
    const page = Number(/const MAX_HOLD_S = (\d+)/.exec(txt)?.[1])
    expect(Number.isFinite(backend)).toBe(true)
    expect(page, '页面上限与 mic_capture.MAX_HOLD_SECONDS 漂了').toBe(backend)
  })

  it('两种录音路径都在，而且回退不是静默的', () => {
    // 安全上下文：页面直接录音；普通 http：调起手机自带录音机。两条必须都在 ——
    // 只有前者的话，http 打开时整页是死的；只有后者的话，https 下白丢体验。
    expect(txt).toContain('navigator.mediaDevices')
    expect(txt).toMatch(/MediaRecorder/)
    expect(txt).toMatch(/<input[^>]+accept="audio\/\*"[^>]*capture/)
    // 回退时必须解释原因，否则用户只会觉得“这按钮坏了”
    expect(txt).toMatch(/安全上下文/)
    expect(txt).toMatch(/HTTP/)
  })

  it('手势的四种结束方式都有归属（早于授权、滑出、取消、到上限）', () => {
    expect(txt).toMatch(/pointerdown/)
    expect(txt).toMatch(/pointerup/)
    expect(txt).toMatch(/pointercancel|lostpointercapture/)
    expect(txt, '松手早于 getUserMedia 时必须有标记，否则整句话被吞').toMatch(/micWantStop/)
    expect(txt).toMatch(/setPointerCapture/)
  })

  it('三个标签页的切换与能力门控都覆盖了 mic', () => {
    expect(txt).toMatch(/for \(const id of \['say', 'mic', 'live'\]\)/)
    expect(txt).toMatch(/id="tab-mic"/)
    // 上传端点属 pet.companion：关掉它时这一页整块不可用，不能让它看起来能点
    expect(txt).toContain("capOff('pet.companion')")
  })
})
