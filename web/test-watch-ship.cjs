#!/usr/bin/env node
/**
 * test-watch-ship.cjs —— 验证 watch-and-ship.cjs 的基本功能
 */
"use strict"

const fs = require("node:fs")
const path = require("node:path")

const ROOT = path.resolve(__dirname, "..")
const WEB_DIR = path.join(ROOT, "web")
const WATCH_SCRIPT = path.join(WEB_DIR, "watch-and-ship.cjs")
const PACKAGE_JSON = path.join(WEB_DIR, "package.json")

let hasError = false

console.log("🔍 测试 watch-and-ship.cjs 环境...\n")

// 1. 检查脚本文件是否存在
if (!fs.existsSync(WATCH_SCRIPT)) {
  console.error("✗ 找不到 watch-and-ship.cjs")
  hasError = true
} else {
  console.log("✓ watch-and-ship.cjs 存在")
}

// 2. 检查 package.json 是否有 watch-ship 脚本
try {
  const pkg = JSON.parse(fs.readFileSync(PACKAGE_JSON, "utf-8"))
  if (!pkg.scripts["watch-ship"]) {
    console.error("✗ package.json 中缺少 'watch-ship' 脚本")
    hasError = true
  } else {
    console.log("✓ package.json 包含 'watch-ship' 脚本")
  }
  
  // 3. 检查 chokidar 依赖
  if (!pkg.devDependencies || !pkg.devDependencies.chokidar) {
    console.error("✗ package.json 中缺少 chokidar devDependency")
    hasError = true
  } else {
    console.log(`✓ chokidar 依赖已添加：${pkg.devDependencies.chokidar}`)
  }
} catch (err) {
  console.error("✗ 读取 package.json 失败:", err.message)
  hasError = true
}

// 4. 检查 src 目录是否存在
const SRC_DIR = path.join(WEB_DIR, "src")
if (!fs.existsSync(SRC_DIR)) {
  console.error("✗ 找不到 web/src/ 目录")
  hasError = true
} else {
  console.log(`✓ web/src/ 目录存在`)
}

// 5. 检查 ship_frontend.cjs 是否存在
const SHIP_SCRIPT = path.join(ROOT, "tools", "ship_frontend.cjs")
if (!fs.existsSync(SHIP_SCRIPT)) {
  console.warn("⚠️  找不到 ship_frontend.cjs（可选依赖）")
} else {
  console.log("✓ ship_frontend.cjs 存在")
}

console.log("\n" + "=".repeat(50))
if (hasError) {
  console.error("❌ 测试失败，请修复上述问题后重试")
  process.exit(1)
} else {
  console.log("✅ 所有检查通过！可以开始使用 npm run watch-ship")
  console.log("\n使用方法:")
  console.log("  cd web")
  console.log("  npm run watch-ship")
  process.exit(0)
}
