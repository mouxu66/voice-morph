// 悬浮声板窗 preload：只暴露「列目录 / 播一声 / 停 / 收起 / 拖动 / 订阅刷新」。
// 渲染层**拿不到任何文件路径**：目录由主进程从 /api/soundboard/catalog 取回，
// 播放只报素材 id（路径解析、错误翻译都在主进程那侧）。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sfx", {
  /** 素材目录（含计数与 icon）→ { ok, items, error } */
  list: () => ipcRenderer.invoke("sfx:list"),
  /** 播一声（非阻塞）→ { ok, duration_s } 或 { ok:false, error } */
  play: (id) => ipcRenderer.invoke("sfx:play", String(id || "")),
  stop: () => ipcRenderer.invoke("sfx:stop"),
  /** 收起窗口（hide，不销毁；下次唤出是热的） */
  hide: () => ipcRenderer.send("sfx:hide"),
  toggle: () => ipcRenderer.send("sfx:toggle"),
  /** 诊断/验收用：{ hotkey, visible, focused, bounds } */
  info: () => ipcRenderer.invoke("sfx:info"),
  // 手动拖拽（app-region 在置顶透明窗上不可靠，见 sfx-window.cjs）
  dragStart: () => ipcRenderer.send("sfx:drag-start"),
  dragMove: () => ipcRenderer.send("sfx:drag-move"),
  dragEnd: () => ipcRenderer.send("sfx:drag-end"),
  /** 主进程在「窗口唤起/首帧」时推来目录（窗口是 hide/show 复用，不重载页面） */
  onRefresh: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("sfx:refresh", handler);
    return () => ipcRenderer.removeListener("sfx:refresh", handler);
  },
});
