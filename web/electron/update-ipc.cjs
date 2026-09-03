// 应用自动更新 IPC + 启动静默检查 —— 从 main.cjs 拆出，行为保持一致。
// 更新源是静态清单 latest.json，地址由环境变量 VM_UPDATE_URL 指定；未配置则完全离线
// （纯本地默认，不发任何网络请求）。详见 web/electron/UPDATE.md。
const { ipcMain } = require("electron");
const updater = require("./updater.cjs");

function registerUpdateIpc() {
  ipcMain.handle("app:version", () => updater.currentVersion());

  ipcMain.handle("update:check", async () => {
    try {
      return await updater.checkForUpdates();
    } catch (e) {
      return { ok: false, configured: true, hasUpdate: false,
        current: updater.currentVersion(), latest: null, reason: String(e.message || e) };
    }
  });

  // 下载：进度用 webContents.send 推给发起请求的那个窗口（前端进度条据此更新）
  ipcMain.handle("update:download", async (event, manifest) => {
    const sender = event.sender;
    const r = await updater.downloadUpdate(manifest, (pct, info) => {
      try {
        sender.send("update:progress", { pct, ...info });
      } catch { /* 窗口已关闭 */ }
    });
    if (r.ok) {
      try {
        sender.send("update:progress", { pct: 100, done: true, file: r.file });
      } catch { /* ignore */ }
    }
    return r;
  });

  ipcMain.handle("update:install", async (_e, file) => updater.installUpdate(file));
  ipcMain.handle("update:skip", async (_e, version) => updater.skipVersion(version));
}

/**
 * 启动后静默检查一次：有更新就主动推给前端弹更新页。
 * 延迟 12s 再做，避免拖慢首屏（后端启动/模型加载才是抢占注意力的那件事）；
 * 失败一律静默 —— 更新检查绝不能打扰正常使用。
 */
function scheduleStartupUpdateCheck(win) {
  const delayMs = 12000;
  setTimeout(async () => {
    try {
      const r = await updater.checkForUpdates();
      if (!r.ok || !r.hasUpdate) return;
      if (!win || win.isDestroyed()) return;
      win.webContents.send("update:available", r);
    } catch {
      /* 静默 */
    }
  }, delayMs);
}

module.exports = { registerUpdateIpc, scheduleStartupUpdateCheck };
