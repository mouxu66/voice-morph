// 应用自动更新 IPC + 启动静默检查 —— 从 main.cjs 拆出，行为保持一致。
// 更新源是静态清单 latest.json，默认指向 GitHub Releases 的最新版永久别名
// （见 updater.cjs 的 DEFAULT_MANIFEST_URL），可用 VM_UPDATE_URL 覆盖；
// 设成 off 则完全不联网（纯本地）。详见 web/electron/UPDATE.md。
const { app, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");
const updater = require("./updater.cjs");

function registerUpdateIpc() {
  // 先把上次更新留下的缓存包收掉（installUpdate 写过"待安装"标记才会动手）。
  // 放在启动时做，是因为安装包被 NSIS 占用期间删不掉（Windows 文件锁）。
  try {
    const swept = updater.sweepDownloadedPackages();
    if (swept.removed.length) {
      console.log(`[update] 已清理更新缓存：${swept.removed.join(", ")}`);
    }
    if (swept.failed.length) {
      console.log(`[update] 更新缓存待下次重试（文件被占用）：${swept.failed.join(", ")}`);
    }
  } catch { /* 清理失败绝不影响启动 */ }

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
 * 测试钩子专用：把检查结果落到 VM_UPDATE_TEST_RESULT 指定的文件。
 * 关键：**无论成功失败都要写**。原先只在下载成功后才写，导致下载失败（更新源不可达、
 * sha256 不匹配、url 解析错等）时测试侧完全看不到证据，只能等到超时报「链路未完成」，
 * 无法区分「没检测到更新」和「检测到了但下载失败」。
 * 正式发布不带 VM_UPDATE_TEST_AUTO，此函数不会被调用。
 */
function writeTestResult(payload) {
  try {
    fs.writeFileSync(
      updater.updateCheckResultPath(),
      JSON.stringify(payload, null, 2),
      "utf-8",
    );
  } catch { /* 写不了也不影响安装流程 */ }
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
  const auto = Boolean(process.env.VM_UPDATE_TEST_AUTO);
  const delayMs = 12000;
  setTimeout(async () => {
    try {
      const r = await updater.checkForUpdates();
      // 测试钩子：检查阶段的失败（连不上更新源 / 清单非法 / 无更新）也要留证据，
      // 否则测试分不清「app 没起来」和「起来了但检查失败」。
      if (auto && (!r.ok || !r.hasUpdate)) {
        writeTestResult({ ...r, stage: "check", auto: true, at: new Date().toISOString() });
        return;
      }
      if (!r.ok || !r.hasUpdate) return;
      if (win && !win.isDestroyed()) win.webContents.send("update:available", r);
      // 测试钩子 VM_UPDATE_TEST_AUTO（默认关闭）：无人值守 VM e2e 自动下载并静默安装，
      // 把检查结果落到 userData/update-check-result.json 供测试断言「收到更新提示」。
      // 正式发布不带此变量，下面整段不执行，行为不变。
      if (auto) {
        try {
          const dl = await updater.downloadUpdate(r.latest);
          if (!dl.ok) {
            // 下载失败：同样落盘，带上失败原因（重要证据，原先被静默吞掉）
            writeTestResult({
              ...r, stage: "download", downloadOk: false,
              error: String(dl.reason || "下载失败"), auto: true, at: new Date().toISOString(),
            });
            return;
          }
          writeTestResult({
            ...r, stage: "download", downloadOk: true, file: dl.file,
            cached: Boolean(dl.cached), auto: true, at: new Date().toISOString(),
          });
          updater.installUpdate(dl.file);
        } catch (e) {
          writeTestResult({
            ...r, stage: "download", downloadOk: false,
            error: `异常：${String((e && e.message) || e)}`, auto: true, at: new Date().toISOString(),
          });
        }
      }
    } catch (e) {
      // 检查本身抛异常（网络层未捕获错误）：也留证据
      if (auto) {
        writeTestResult({
          ok: false, configured: true, hasUpdate: false,
          current: updater.currentVersion(), latest: null,
          stage: "check", error: String((e && e.message) || e),
          auto: true, at: new Date().toISOString(),
        });
      }
    }
  }, delayMs);
}

module.exports = { registerUpdateIpc, scheduleStartupUpdateCheck };
