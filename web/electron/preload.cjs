// 主窗口 preload：把后端「启动/停止/状态/日志」+ 应用自动更新能力暴露给渲染层（window.electron）。
// 仅本地桌面端注入；网页/Vite/局域网模式下 window.electron 为 undefined，前端自动降级。
const { contextBridge, ipcRenderer } = require("electron");

// 订阅类 API 统一返回「取消订阅」函数，前端 useEffect 里直接 return 即可
function on(channel, cb) {
  const handler = (_e, payload) => cb(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

contextBridge.exposeInMainWorld("electron", {
  startBackend: () => ipcRenderer.invoke("backend:start"),
  stopBackend: () => ipcRenderer.invoke("backend:stop"),
  /** 重启后端（改完模型配置后用；内部处理端口释放等待） */
  restartBackend: () => ipcRenderer.invoke("backend:restart"),
  backendStatus: () => ipcRenderer.invoke("backend:status"),
  showBackendLog: () => ipcRenderer.invoke("backend:show-log"),
  // 页面导览：切页时把该页的介绍与动作推给桌宠窗口（fire-and-forget）
  petGuide: (payload) => ipcRenderer.send("pet:guide", payload),

  // ---- 模型配置 / 首启引导 ----
  /** 只读状态：{ config, configPath, items, ttsOk, rvcOk, allOk, missing, setupSeen, petGuideSeen } */
  setupStatus: () => ipcRenderer.invoke("setup:status"),
  /** 弹目录选择器（tts_models / tts_venv / rvc_root），返回 { canceled, path, ok, reason } */
  setupPickDir: (kind) => ipcRenderer.invoke("setup:pick-dir", kind),
  /** 保存配置（部分字段亦可），返回保存后的最新状态 */
  setupSave: (patch) => ipcRenderer.invoke("setup:save", patch),
  /** 稍后配置：不再自动弹引导 */
  setupDismiss: () => ipcRenderer.invoke("setup:dismiss"),
  /** 重新走一遍配置向导（设置面板入口） */
  setupRunWizard: () => ipcRenderer.invoke("setup:run-wizard"),
  /** 清空路径配置 */
  setupReset: () => ipcRenderer.invoke("setup:reset"),
  /** 在资源管理器中定位 config.json */
  setupShowConfig: () => ipcRenderer.invoke("setup:show-config"),

  // ---- 应用自动更新 ----
  appVersion: () => ipcRenderer.invoke("app:version"),
  /** 检查更新：返回 { ok, configured, hasUpdate, current, latest, reason } */
  updateCheck: () => ipcRenderer.invoke("update:check"),
  /** 下载安装包（manifest 为 updateCheck 返回的 latest），进度走 onUpdateProgress */
  updateDownload: (manifest) => ipcRenderer.invoke("update:download", manifest),
  /** 拉起安装程序并退出应用 */
  updateInstall: (file) => ipcRenderer.invoke("update:install", file),
  /** 跳过某个版本，之后不再自动弹 */
  updateSkip: (version) => ipcRenderer.invoke("update:skip", version),
  /** 下载进度：{ pct, received, total, done? } */
  onUpdateProgress: (cb) => on("update:progress", cb),
  /** 启动静默检查发现新版本时推来：{ hasUpdate, current, latest } */
  onUpdateAvailable: (cb) => on("update:available", cb),
});
