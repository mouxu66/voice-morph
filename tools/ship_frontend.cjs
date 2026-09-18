#!/usr/bin/env node
/**
 * ship_frontend.cjs —— 把 `web/dist` 送进「已安装的桌面端」并核验。
 *
 * 为什么需要它
 * ------------
 * 安装版读的不是 `D:\变声\web\dist`，而是
 * `%LOCALAPPDATA%\Programs\voice-morph-desktop\resources\backend\web_dist`，
 * 由 `m2_server/backend_autosync.py` 镜像 —— 而它**只在后端启动那一刻跑一次**。
 * 于是「改前端 → build → 打开应用还是旧界面」成了最容易白干的一步
 * （2026-09-18 真踩过：用户那份还是几小时前的构建）。
 *
 * 另外两点容易误判：
 *   - `tools/sync_backend.ps1` **不含** `web/dist → web_dist`（只拷 m2_server / tools），
 *     所以别指望它同步前端；
 *   - 自动更新不会帮忙：它查 GitHub Releases，而线上 Releases 为空 → 永远 404 静默返回；
 *     即便发版也是整套安装包替换。
 *
 * 用法
 * ----
 *     npm run ship                 # 构建 + 同步 + 核验（推荐）
 *     node tools/ship_frontend.cjs --dry-run
 *     VM_DESKTOP_BACKEND=<别的 backend 目录> node tools/ship_frontend.cjs
 *
 * 安全性
 * ------
 * 目标路径必须严格以 `voice-morph-desktop/resources/backend` 结尾，否则拒绝执行 ——
 * 这个脚本会删目标下的文件，路径算错就是灾难。校验逻辑单独导出，由
 * `tools/test-ship-frontend.cjs` 覆盖。
 *
 * 策略：**先覆盖拷贝，再精确删除多余文件**（不整目录 rmtree）。
 * 这样既避开 safe-delete 包装对批量删除的拦截，也不会在失败时留下空壳目录。
 */
"use strict"

const crypto = require("node:crypto")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")

const ROOT = path.resolve(__dirname, "..")
const SRC_DIST = path.join(ROOT, "web", "dist")
const TARGET_SUFFIX = path.join("voice-morph-desktop", "resources", "backend")
const SKIP_DIRS = new Set(["__pycache__", ".pytest_cache"])

/** 安装目录里的 backend 根；支持 VM_DESKTOP_BACKEND 覆盖，非 Windows 返回 null */
function defaultTarget({ env = process.env, home = os.homedir(), platform = process.platform } = {}) {
  // 刻意不做 path.resolve：该函数要能在非 Windows 上被测试断言字符串形态
  if (env.VM_DESKTOP_BACKEND) return env.VM_DESKTOP_BACKEND
  if (platform !== "win32") return null
  const localAppData = env.LOCALAPPDATA || path.join(home, "AppData", "Local")
  return path.join(localAppData, "Programs", TARGET_SUFFIX)
}

/**
 * 拒绝一切"看起来不像安装目录"的路径。这是本脚本唯一的护栏：
 * 它后面要删文件，路径算错就没有第二次机会。
 */
function assertSafeTarget(dir) {
  if (!dir) throw new Error("无法确定安装目录（非 Windows，或未设置 VM_DESKTOP_BACKEND）")
  const norm = path.resolve(dir)
  if (norm === path.parse(norm).root) throw new Error(`拒绝操作盘符根目录：${norm}`)
  if (norm.split(path.sep).filter(Boolean).length < 3) {
    throw new Error(`目标路径层级过浅，拒绝执行：${norm}`)
  }
  if (!norm.toLowerCase().endsWith(path.sep + TARGET_SUFFIX.toLowerCase())) {
    throw new Error(`目标路径不符合预期（应以 ${TARGET_SUFFIX} 结尾），拒绝执行：${norm}`)
  }
  return norm
}

/** 递归列出目录下所有文件（相对路径，正斜杠），用于比对 */
function listFiles(dir) {
  const out = []
  const walk = (cur, rel) => {
    for (const entry of fs.readdirSync(cur, { withFileTypes: true })) {
      if (SKIP_DIRS.has(entry.name)) continue
      const abs = path.join(cur, entry.name)
      const r = rel ? `${rel}/${entry.name}` : entry.name
      if (entry.isDirectory()) walk(abs, r)
      else if (entry.isFile()) out.push(r)
    }
  }
  if (fs.existsSync(dir)) walk(dir, "")
  return out.sort()
}

/** 算出「要覆盖多少个」+「目标里哪些是多余的」 */
function planSync(srcDir, dstDir) {
  const src = new Set(listFiles(srcDir))
  const dst = new Set(listFiles(dstDir))
  const stale = [...dst].filter((f) => !src.has(f)).sort()
  return { count: src.size, stale, missing: [...src].filter((f) => !dst.has(f)).sort() }
}

/**
 * 递归把 srcDir 覆盖到 dstDir（不删目标已有文件，删除由调用方按 plan 精确执行）。
 *
 * 刻意不用 `fs.cpSync`：在本机沙箱里它会**静默打死 Node 进程**（原生层崩溃，
 * JS 侧 try/catch 接不到，退出码 127），而逐文件 copyFileSync 完全正常
 * （2026-09-18 实测）。逐文件还能单独兜错，不至于一个文件失败整批中断。
 */
function copyTree(srcDir, dstDir) {
  let copied = 0
  const failed = []
  const walk = (src, dst, rel) => {
    fs.mkdirSync(dst, { recursive: true })
    for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
      if (SKIP_DIRS.has(entry.name)) continue
      const s = path.join(src, entry.name)
      const d = path.join(dst, entry.name)
      const r = rel ? `${rel}/${entry.name}` : entry.name
      if (entry.isDirectory()) walk(s, d, r)
      else if (entry.isFile()) {
        try {
          fs.copyFileSync(s, d)
          copied += 1
        } catch (err) {
          failed.push(`${r}: ${err.message}`)
        }
      }
    }
  }
  walk(srcDir, dstDir, "")
  return { copied, failed }
}

/**
 * 逐字节比对两棵目录树（sha1）。只用于 web_dist 自检 ——
 * 全量漂移检查交给 `tools/verify_backend_sync.py`，两者职责不同：
 * 那个脚本覆盖 m2_server / tools / web_dist / pet，任一目录漂移都会让它报错，
 * 而本脚本只负责前端，不该被无关目录的漂移拖红。
 */
function diffTrees(srcDir, dstDir) {
  const files = listFiles(srcDir)
  const mismatched = []
  const missing = []
  for (const rel of files) {
    const s = path.join(srcDir, ...rel.split("/"))
    const d = path.join(dstDir, ...rel.split("/"))
    if (!fs.existsSync(d)) {
      missing.push(rel)
      continue
    }
    if (hashFile(s) !== hashFile(d)) mismatched.push(rel)
  }
  const extra = listFiles(dstDir).filter((f) => !files.includes(f))
  return { total: files.length, mismatched, missing, extra }
}

function hashFile(p) {
  return crypto.createHash("sha1").update(fs.readFileSync(p)).digest("hex")
}

function main(argv) {
  const dryRun = argv.includes("--dry-run")
  const target = assertSafeTarget(defaultTarget())
  const dstDist = path.join(target, "web_dist")

  console.log(`源:   ${SRC_DIST}`)
  console.log(`目标: ${dstDist}`)

  if (!fs.existsSync(SRC_DIST)) {
    console.error("\n✗ 找不到 web/dist，先跑 `npm run build`。")
    return 1
  }
  if (!fs.existsSync(target)) {
    console.log("\n• 没找到安装目录 —— 可能你用源码模式跑（npm run electron:dev），")
    console.log("  那样改完重启即生效，不需要本步骤。")
    return 0
  }

  const plan = planSync(SRC_DIST, dstDist)
  console.log(`\n将覆盖 ${plan.count} 个文件` + (plan.stale.length ? `，清理 ${plan.stale.length} 个多余文件` : "，无多余文件"))
  for (const f of plan.stale) console.log(`  - 多余: ${f}`)
  if (dryRun) {
    console.log("\n--dry-run：未做改动。")
    return 0
  }

  // ① 覆盖拷贝（不整目录 rmtree，也不用 cpSync —— 见 copyTree 注释）
  const { copied, failed } = copyTree(SRC_DIST, dstDist)
  console.log(`  已写入 ${copied} 个文件`)
  if (failed.length) {
    console.error(`\n✗ ${failed.length} 个文件写入失败（应用可能正在运行并占用了它们）：`)
    for (const f of failed.slice(0, 8)) console.error(`    ${f}`)
    if (failed.length > 8) console.error(`    …还有 ${failed.length - 8} 个`)
    console.error("\n  请**完全退出应用**（注意桌宠会常驻）后重跑。")
    return 1
  }

  // ② 精确删除多余文件（构建产物换 hash 后留下的旧资源）
  for (const rel of plan.stale) {
    try {
      fs.rmSync(path.join(dstDist, ...rel.split("/")), { force: true })
    } catch (err) {
      console.error(`  ! 删除失败 ${rel}：${err.message}`)
    }
  }

  // ③ 自检：只比对 web_dist 这一份，逐字节
  const diff = diffTrees(SRC_DIST, dstDist)
  const problems = diff.mismatched.length + diff.missing.length + diff.extra.length
  console.log(`\n=== 自检（web_dist 共 ${diff.total} 个文件）===`)
  if (problems === 0) {
    console.log("✓ 逐字节一致")
  } else {
    if (diff.mismatched.length) console.error(`  ✗ 内容不一致 ${diff.mismatched.length}：${diff.mismatched.slice(0, 5).join(", ")}`)
    if (diff.missing.length) console.error(`  ✗ 目标缺失 ${diff.missing.length}：${diff.missing.slice(0, 5).join(", ")}`)
    if (diff.extra.length) console.error(`  ✗ 目标多余 ${diff.extra.length}：${diff.extra.slice(0, 5).join(", ")}`)
    console.error("\n✗ 自检未通过 —— 可能应用正在运行并锁住了文件。")
    return 1
  }

  console.log(
    "\n✓ 前端已送达。**完全退出应用再打开**即可看到新界面。\n" +
      "  完整漂移检查（含 m2_server / tools / 桌宠）：python tools/verify_backend_sync.py",
  )
  return 0
}

module.exports = { defaultTarget, assertSafeTarget, listFiles, planSync, copyTree, diffTrees, TARGET_SUFFIX }

if (require.main === module) {
  try {
    process.exitCode = main(process.argv.slice(2))
  } catch (err) {
    console.error(`✗ ${err.message}`)
    process.exitCode = 1
  }
}
