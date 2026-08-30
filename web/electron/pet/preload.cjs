// 桌宠窗口 preload：只暴露点击穿透开关与手动拖拽给渲染器
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pet", {
  setIgnoreMouse: (v) => ipcRenderer.send("pet:ignore-mouse", !!v),
  // 手动拖拽：渲染器报告按下/移动，主进程用光标屏幕坐标挪窗口
  dragStart: () => ipcRenderer.send("pet:drag-start"),
  dragMove: () => ipcRenderer.send("pet:drag-move"),
  dragEnd: () => ipcRenderer.send("pet:drag-end"),
});
