/**
 * build-watch.cjs —— 构建监听器 IPC 控制
 * 
 * 功能：
 * - 启动/停止自动构建监听器
 * - 查询监听器状态
 * - 在应用设置中显示状态
 */
"use strict"

const { spawn } = require("child_process")
const path = require("path")
const fs = require("fs")

let watchProcess = null
let isWatching = false

const WATCH_SCRIPT = path.join(__dirname, "..", "web", "watch-and-ship.cjs")
const PID_FILE = path.join(__dirname, "..", "web", ".watch-ship.pid")

/** 检查监听器是否正在运行 */
function isWatcherRunning() {
  if (!watchProcess) return false
  
  try {
    // 检查进程是否存在
    const out = require("child_process").execFileSync(
      "powershell",
      ["-NoProfile", "-Command", 
        `(Get-Process | Where-Object {$_.Id -eq ${watchProcess.pid}} -ErrorAction SilentlyContinue).Id`],
      { windowsHide: true, timeout: 2000 }
    ).toString().trim()
    
    return out === String(watchProcess.pid)
  } catch {
    return false
  }
}

/** 启动监听器 */
function startWatcher() {
  if (isWatching && isWatcherRunning()) {
    return { success: false, message: "监听器已在运行" }
  }
  
  // 如果旧进程还在，先清理
  if (watchProcess && !isWatcherRunning()) {
    try { watchProcess.kill("SIGTERM") } catch {}
    watchProcess = null
  }
  
  try {
    const webDir = path.join(__dirname, "..", "web")
    
    console.log("[build-watch] 启动前端自动构建监听器...")
    
    watchProcess = spawn("node", [WATCH_SCRIPT], {
      cwd: webDir,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
      detached: false,
    })
    
    // 记录 PID
    if (watchProcess.pid) {
      fs.writeFileSync(PID_FILE, String(watchProcess.pid), "utf-8")
    }
    
    // 监听输出
    watchProcess.stdout.on("data", (data) => {
      const text = data.toString()
      console.log(`[build-watch] ${text.trim()}`)
      
      // 检测是否成功启动
      if (text.includes("开始监听")) {
        isWatching = true
      }
    })
    
    watchProcess.stderr.on("data", (data) => {
      console.error(`[build-watch err] ${data.toString().trim()}`)
    })
    
    watchProcess.on("exit", (code) => {
      console.log(`[build-watch] 监听器退出，码=${code}`)
      isWatching = false
      try { fs.unlinkSync(PID_FILE) } catch {}
      
      // 如果是异常退出（非用户主动停止），尝试重启
      if (code !== 0 && code !== null) {
        console.warn("[build-watch] 监听器异常退出，尝试重启...")
        setTimeout(() => startWatcher(), 3000)
      }
    })
    
    watchProcess.on("error", (err) => {
      console.error("[build-watch] 启动失败:", err.message)
      isWatching = false
      try { fs.unlinkSync(PID_FILE) } catch {}
    })
    
    return { 
      success: true, 
      message: "监听器已启动",
      pid: watchProcess.pid 
    }
  } catch (err) {
    console.error("[build-watch] 启动异常:", err)
    return { success: false, message: `启动失败：${err.message}` }
  }
}

/** 停止监听器 */
function stopWatcher() {
  if (!watchProcess) {
    return { success: true, message: "监听器未运行" }
  }
  
  try {
    console.log("[build-watch] 停止监听器...")
    
    // Windows 进程树终止
    if (watchProcess.pid) {
      require("child_process").execFileSync(
        "taskkill",
        ["/PID", String(watchProcess.pid), "/T", "/F"],
        { windowsHide: true, timeout: 5000 }
      )
    }
    
    watchProcess = null
    isWatching = false
    
    try { fs.unlinkSync(PID_FILE) } catch {}
    
    return { success: true, message: "监听器已停止" }
  } catch (err) {
    console.error("[build-watch] 停止失败:", err)
    return { success: false, message: `停止失败：${err.message}` }
  }
}

/** 获取监听器状态 */
function getWatcherStatus() {
  const running = isWatcherRunning()
  
  return {
    isRunning: running,
    pid: watchProcess?.pid || null,
    scriptPath: WATCH_SCRIPT,
    hasScript: fs.existsSync(WATCH_SCRIPT),
  }
}

module.exports = {
  startWatcher,
  stopWatcher,
  getWatcherStatus,
}
