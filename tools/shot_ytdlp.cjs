/**
 * 「在线扒歌」页视觉验收：三种环境状态 + 亮暗两主题 + 窄窗口。
 *
 * 跑之前先起两个服务：
 *   后端  VM_BACKEND_AUTOSYNC=0 python m2_server/server.py        （默认 :8000）
 *   前端  cd web && npx vite --port 5199
 *
 * 为什么连着真后端：这个页面的首屏要 `/api/plugins` 才有路由、要 `/api/health`
 * 才不白屏。挨个喂替身既费事又失真 —— 只有 `/ytdlp/status` 这一条用替身，
 * 因为"装上 / 没装"两种形态都得看到，本机只可能是其中一种。
 *
 * ⚠️ 拦截器**不能**写成 `**\/api\/**`：那会连 vite 开发期的 `/src/api/client.ts`
 * 一起吞掉，返回 JSON 后浏览器按 MIME 拒绝执行，页面直接白屏。
 *
 * 用法：node tools/shot_ytdlp.cjs [frontendUrl]
 */
const path = require("path")
const fs = require("fs")
const { createRequire } = require("module")

// playwright 装在 web/ 下，本脚本住在 tools/ —— 直接 require 解析不到。
// 用 createRequire 显式指到 web/ 解析，别去改 NODE_PATH（那只对子进程生效）。
const webRequire = createRequire(path.join(__dirname, "..", "web", "package.json"))
const { chromium } = webRequire("playwright")

const BASE = process.argv[2] || "http://localhost:5199"
const OUT = path.join(__dirname, "..", "..", "tmp", "ytdlp-shots")

const SITES = [
  { name: "Free Music Archive", example: "freemusicarchive.org" },
  { name: "Jamendo", example: "www.jamendo.com" },
  { name: "QQ音乐", example: "y.qq.com" },
  { name: "哔哩哔哩", example: "www.bilibili.com" },
  { name: "哔哩哔哩（短链）", example: "b23.tv" },
  { name: "咪咕音乐", example: "music.migu.cn" },
  { name: "喜马拉雅", example: "www.ximalaya.com" },
  { name: "网易云音乐", example: "music.163.com" },
]

const REAL_PATH =
  "C:\\Users\\mouxu\\AppData\\Local\\Packages\\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\\LocalCache\\local-packages\\Python313\\Scripts\\yt-dlp.exe"

const STATES = {
  ready: { available: true, path: REAL_PATH, version: "2026.03.17", sites: SITES, hint: "" },
  missing: {
    available: false,
    path: "",
    version: "",
    sites: SITES,
    hint: "没找到 yt-dlp，拿不到这条链接的音频。装好后用环境变量 VM_YTDLP 指向它，或直接放进 PATH，然后重启后端。",
  },
}

/**
 * 关掉可能挡在前面的首启向导。
 *
 * 首次进入会依次弹两个模态（用例选择 + 新手引导），它们会盖住整页截图。
 * 直接写它们的 localStorage 标记比去点按钮稳 —— 键名在源码里是常量：
 *   vm-use-asked / vm-use-profile  → lib/useProfile.ts
 *   vm_first_launch_done           → components/FirstLaunchGuide.tsx
 */
async function dismissOnboarding(page) {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("vm-use-asked", "1")
      localStorage.setItem("vm-use-profile", "all")
      localStorage.setItem("vm_first_launch_done", "1")
    } catch {
      /* 隐私模式下 localStorage 可能不可用，忽略 */
    }
  })
}

/** 只拦这一条：其余全放给真后端。 */
async function stubStatus(page, state) {
  await page.route(/\/api\/ytdlp\/status/, (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(state) }),
  )
}

async function open(browser, { width, height, dark, state, fill }) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  await dismissOnboarding(page)
  await stubStatus(page, state)
  await page.goto(`${BASE}/#/ytdlp`, { waitUntil: "domcontentloaded" })
  if (dark) await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"))
  await page.waitForSelector('input[aria-label="歌曲链接"]', { timeout: 20000 })

  // 向导若仍在前台，按键关掉；关不掉也不阻塞（截图里能看出来）
  const closer = page.getByRole("button", { name: /先逛逛|跳过/ })
  if (await closer.count()) {
    await closer.first().click({ timeout: 3000 }).catch(() => {})
  }
  await page.waitForTimeout(700)
  if (fill) {
    await page.fill('input[aria-label="歌曲链接"]', "https://music.163.com/song?id=1978534")
    await page.waitForTimeout(350)
  }
  return { ctx, page }
}

async function shoot(browser, name, opts) {
  const { ctx, page } = await open(browser, opts)
  await page.screenshot({ path: path.join(OUT, name), fullPage: true })
  await ctx.close()
  console.log("  ✓", name)
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  console.log("截图输出：", OUT)

  const D = { width: 1440, height: 1000 }
  await shoot(browser, "01-light-ready.png", { ...D, dark: false, state: STATES.ready, fill: true })
  await shoot(browser, "02-dark-ready.png", { ...D, dark: true, state: STATES.ready, fill: true })
  await shoot(browser, "03-light-missing.png", { ...D, dark: false, state: STATES.missing })
  await shoot(browser, "04-dark-missing.png", { ...D, dark: true, state: STATES.missing })
  await shoot(browser, "05-mobile-ready.png", { width: 414, height: 1000, dark: false, state: STATES.ready, fill: true })

  await browser.close()
})()
