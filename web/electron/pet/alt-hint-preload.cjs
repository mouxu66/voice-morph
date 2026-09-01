// 置顶提示横幅 preload：只暴露订阅接口（主进程推送阶段与倒计时）
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("altHint", {
  onHint: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("alt-hint:update", handler);
    return () => ipcRenderer.removeListener("alt-hint:update", handler);
  },
});
