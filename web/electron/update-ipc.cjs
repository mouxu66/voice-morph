// 应用自动更新 IPC + 启动静默检查 —— 从 main.cjs 拆出，行为保持一致。
// 更新源是静态清单 latest.json，地址由环境变量 VM_UPDATE_URL 指定；未配置则完全离线
// （纯本地默认，不发任何网络请求）。详见 web/electron/UPDATE.md。
const { app, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");
const updater = require("./updater.cjs");

function registerUpdateIpc() {
  ipcMain.handle("app:version", () => updater.currentVersion());

  ipcMain.handle("update:check", async () => {
    // 开发（源码）模式不检查更新：与 scheduleStartupUpdateCheck 同一守卫，双入口统一。
    // 否则 dev server 页面点“检查更新”会真的发网络请求（本机若配了 VM_UPDATE_URL）。
    if (!app.isPackaged) {
      return {
        ok: true, configured: false, hasUpdate: false,
        current: updater.currentVersion(), latest: null,
        reason: "开发模式不检查更新",
      };
    }
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
  // 开发（源码）模式一律不检查更新，避免 dev server / 开发机被更新弹窗打扰；
  // 只有打包安装版（app.isPackaged）才启用启动静默检查。
  if (!app.isPackaged) return;
  const delayMs = 12000;
  setTimeout(async () => {
    try {
      const r = await updater.checkForUpdates();
      if (!r.ok || !r.hasUpdate) return;
      if (win && !win.isDestroyed()) win.webContents.send("update:available", r);
      // 测试钩子 VM_UPDATE_TEST_AUTO（默认关闭）：无人值守 VM e2e 自动下载并静默安装，
      // 把检查结果落到 userData/update-check-result.json 供测试断言「收到更新提示」。
      // 正式发布不带此变量，下面整段不执行，行为不变。
      if (process.env.VM_UPDATE_TEST_AUTO) {
        try {
          const dl = await updater.downloadUpdate(r.latest);
          if (!dl.ok) return;
          const result = { ...r, file: dl.file, auto: true, at: new Date().toISOString() };
          try {
            fs.writeFileSync(
              path.join(app.getPath("userData"), "update-check-result.json"),
              JSON.stringify(result, null, 2),
              "utf-8",
            );
          } catch { /* 写不了也不影响安装 */ }
          updater.installUpdate(dl.file);
        } catch {
          /* 静默 */
        }
      }
    } catch {
      /* 静默 */
    }
  }, delayMs);
}

module.exports = { registerUpdateIpc, scheduleStartupUpdateCheck };
