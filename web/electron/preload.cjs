// 主窗口 preload：把后端「启动/停止/状态/日志」能力暴露给渲染层（window.electron）。
// 仅本地桌面端注入；网页/Vite/局域网模式下 window.electron 为 undefined，前端自动降级。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electron", {
  startBackend: () => ipcRenderer.invoke("backend:start"),
  stopBackend: () => ipcRenderer.invoke("backend:stop"),
  backendStatus: () => ipcRenderer.invoke("backend:status"),
  showBackendLog: () => ipcRenderer.invoke("backend:show-log"),
  // 页面导览：切页时把该页的介绍与动作推给桌宠窗口（fire-and-forget）
  petGuide: (payload) => ipcRenderer.send("pet:guide", payload),
});
