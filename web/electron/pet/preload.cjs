// 桌宠窗口 preload：只暴露点击穿透开关、手动拖拽与页面导览订阅给渲染器
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pet", {
  setIgnoreMouse: (v) => ipcRenderer.send("pet:ignore-mouse", !!v),
  // 手动拖拽：渲染器报告按下/移动，主进程用光标屏幕坐标挪窗口
  dragStart: () => ipcRenderer.send("pet:drag-start"),
  dragMove: () => ipcRenderer.send("pet:drag-move"),
  dragEnd: () => ipcRenderer.send("pet:drag-end"),
  // 页面导览：主进程在切页时把「页面介绍 + 动作编排」推过来
  onGuide: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("pet:guide", handler);
    return () => ipcRenderer.removeListener("pet:guide", handler);
  },
  // 试听结果：合成完成回传 { ok, wav, url, duration_s } 或 { ok:false, error }
  onPreviewResult: (cb) => ipcRenderer.on("pet:preview-result", (_e, r) => cb(r)),
  // 发送结果：面板状态行直接显示 ✓/✗
  onSendResult: (cb) => ipcRenderer.on("pet:send-result", (_e, r) => cb(r)),
  // 快捷面板：单击角色发最近合成 / 输入文字合成后发送（可指定音色）/ 历史重发 / 实时变声开关
  sendLast: () => ipcRenderer.send("pet:send-last"),
  sendText: (text, voiceId) => ipcRenderer.send("pet:send-text", String(text || "").slice(0, 500), String(voiceId || "")),
  sendWav: (wav) => ipcRenderer.send("pet:send-wav", String(wav || "")),
  previewText: (text, voiceId) => ipcRenderer.send("pet:preview", String(text || "").slice(0, 500), String(voiceId || "")),
  liveToggle: () => ipcRenderer.send("pet:live-toggle"),
  // 查看后端日志：后端离线时在气泡上点一下，主进程在文件管理器里定位 backend.log
  showBackendLog: () => ipcRenderer.invoke("backend:show-log"),
});
