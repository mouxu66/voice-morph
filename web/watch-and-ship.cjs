#!/usr/bin/env node
/**
 * watch-and-ship.cjs —— 前端文件变更监听 + 自动构建 + 同步到安装版
 * 
 * 用法：
 *   npm run watch-ship    # 启动监听（需要应用已安装）
 *   npm run watch-ship -- --dev   # 仅构建不同步（开发调试用）
 * 
 * 原理：
 *   - 使用 chokidar 监听 web/src 目录变化
 *   - 检测到变化后等待 2 秒防抖
 *   - 自动执行 `npm run build`
 *   - 构建成功后调用 ship_frontend.cjs 同步到安装目录
 *   - 提示用户重启应用或热重载（如果支持）
 */
"use strict"

const { spawn } = require("child_process")
const fs = require("node:fs")
const path = require("node:path")
const os = require("node:os")

// 依赖检查
let chokidar
try {
  chokidar = require("chokidar")
} catch (err) {
  console.error("✗ 缺少 chokidar 依赖，请先运行：npm install chokidar --save-dev")
  process.exit(1)
}

const ROOT = path.resolve(__dirname, "..")
const WEB_DIR = path.join(ROOT, "web")
const SRC_DIR = path.join(WEB_DIR, "src")
const DIST_DIR = path.join(WEB_DIR, "dist")

/** 安装目录里的 backend 根 */
function getDesktopBackend() {
  if (process.env.VM_DESKTOP_BACKEND) return process.env.VM_DESKTOP_BACKEND
  if (process.platform !== "win32") return null
  const localAppData = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local")
  return path.join(localAppData, "Programs", "voice-morph-desktop", "resources", "backend")
}

/** 运行一个命令并返回是否成功 */
function runCommand(cmd, args, cwd) {
  return new Promise((resolve, reject) => {
    console.log(`\n🚀 执行：${cmd} ${args.join(" ")}`)
    const proc = spawn(cmd, args, {
      cwd,
      stdio: "inherit",
      windowsHide: true,
    })
    proc.on("close", (code) => {
      if (code === 0) resolve(true)
      else reject(new Error(`${cmd} 失败，退出码 ${code}`))
    })
    proc.on("error", reject)
  })
}

/** 同步前端到安装版 */
async function shipToDesktop(dryRun = false) {
  const shipScript = path.join(ROOT, "tools", "ship_frontend.cjs")
  if (!fs.existsSync(shipScript)) {
    console.warn(`⚠️  找不到 ship_frontend.cjs: ${shipScript}`)
    return false
  }

  const target = getDesktopBackend()
  if (!target) {
    console.log("ℹ️  非 Windows 平台或未安装应用，跳过同步步骤")
    return true
  }

  console.log(`\n📦 同步前端到：${target}`)
  
  const shipProc = spawn(process.execPath, [shipScript], {
    cwd: WEB_DIR,
    stdio: "inherit",
    windowsHide: false, // 让 ship 脚本自己处理输出
  })
  
  return new Promise((resolve) => {
    shipProc.on("close", (code) => {
      resolve(code === 0)
    })
  })
}

/** 防抖定时器 */
let debounceTimer = null
function scheduleBuild() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(async () => {
    console.log("\n⏱️  文件变化稳定，开始构建...")
    try {
      // ① 构建
      await runCommand("npm", ["run", "build"], WEB_DIR)
      
      // ② 同步（除非 --dev 模式）
      const devMode = process.argv.includes("--dev")
      if (!devMode) {
        await shipToDesktop()
      }
      
      console.log("\n✅ 构建完成！")
      if (devMode) {
        console.log("ℹ️  开发模式：未同步到安装版（使用 --dev 禁用）")
      } else {
        console.log("🎉 已同步到安装版，请完全退出应用再打开以查看更新")
      }
    } catch (err) {
      console.error("\n✗ 构建失败:", err.message)
    } finally {
      debounceTimer = null
    }
  }, 2000) // 2 秒防抖
}

function main() {
  console.log("👀 开始监听前端文件变化...")
  console.log(`📂 监听目录：${SRC_DIR}`)
  
  if (!fs.existsSync(SRC_DIR)) {
    console.error(`\n✗ 找不到源目录：${SRC_DIR}`)
    console.error("请确保在 web/ 目录下运行此脚本")
    return
  }

  // 监听 .tsx/.ts/.css 等文件
  const watcher = chokidar.watch(SRC_DIR, {
    persistent: true,
    ignoreInitial: true,
    awaitWriteFinish: {
      stabilityThreshold: 2000, // 等待文件写入完成
      pollInterval: 500,
    },
    ignored: [/^node_modules/, /\.git/, /__pycache__/],
  })

  watcher
    .on("change", (filePath) => {
      console.log(`\n📝 检测到文件变化：${path.relative(SRC_DIR, filePath)}`)
      scheduleBuild()
    })
    .on("error", (err) => {
      console.error("\n✗ 监听器错误:", err.message)
    })

  console.log("\n💡 提示:")
  console.log("  - 按 Ctrl+C 停止监听")
  console.log("  - 使用 --dev 参数可仅构建不同步")
  console.log("\n─────────────────────────────────────")
}

// 启动
main()
