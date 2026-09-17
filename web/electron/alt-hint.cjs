// 置顶提示横幅（发微信语音时的分阶段引导）—— 从 main.cjs 拆出，行为保持一致。
// 桌宠气泡太小，用户切到微信窗口后看不见、也不知道什么时候该按 Alt。
// 这里用独立的置顶透明窗口，横跨屏幕顶部居中显示大号引导 + 倒计时 + 进度条，
// 全程浮在微信之上、鼠标点击穿透，不挡任何操作。
const { BrowserWindow, screen, globalShortcut } = require("electron");
const path = require("path");
const fs = require("fs");
const backend = require("./backend.cjs");
const { backendPost } = backend;

// 加载 pet 资源，同 pet.cjs：生产（安装版）自动回退 asar 内置副本（随版本更新），不读 D:\变声 旧源码。
const _projectPetDir = path.join(backend.resolveProjectRoot(), "web", "electron", "pet");
const PET_DIR = fs.existsSync(path.join(_projectPetDir, "pet.html")) ? _projectPetDir : path.join(__dirname, "pet");
let altHintWin = null;
let altHintReady = false;
let altHintTimer = null;
let altHintPending = null;   // 窗口未加载完时缓存最后一条，did-finish-load 后补发

// 全局「退出录制」热键：仅在录音引导横幅显示期间注册 Esc，
// 用户误触发语音后按 Esc 即可中止播放 + 隐藏横幅（见 abortRecording）。
let cancelArmed = false;

function armCancelKey() {
  if (cancelArmed) return;
  try {
    if (globalShortcut.register("Escape", abortRecording)) cancelArmed = true;
  } catch {}
}

function disarmCancelKey() {
  if (!cancelArmed) return;
  try { globalShortcut.unregister("Escape"); } catch {}
  cancelArmed = false;
}

/** 用户按 Esc：中止当前微信语音录制引导（停止播放 + 隐藏横幅 + 提示已取消）。 */
function abortRecording() {
  disarmCancelKey();
  hideAltHint();
  // 通知后端停止向 CABLE 播放（无进行中的播放时后端会安全返回）
  try { backendPost("/api/wechat/stop_play", {}, () => {}, 5000); } catch {}
  // 桌宠气泡提示已取消（pet.cjs 循环 require，运行时取缓存模块）
  try {
    require("./pet.cjs").showPetGuide({
      title: "已取消",
      lines: ["已停止发送，声卡会自动还原"],
      action: "idle", motion: "nod", duration: 5000,
    });
  } catch {}
}

function ensureAltHintWindow() {
  if (altHintWin && !altHintWin.isDestroyed()) return;
  const { workArea } = screen.getPrimaryDisplay();
  const width = 560, height = 190;
  const x = workArea.x + Math.round((workArea.width - width) / 2);
  const y = workArea.y + Math.round(workArea.height * 0.04);
  altHintWin = new BrowserWindow({
    width, height, x, y,
    transparent: true, frame: false, resizable: false,
    alwaysOnTop: true, skipTaskbar: true, hasShadow: false,
    focusable: false, show: false, // 不抢焦点，用 showInactive 静默展示
    webPreferences: {
      preload: path.join(PET_DIR, "alt-hint-preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  altHintWin.setAlwaysOnTop(true, "screen-saver");
  altHintWin.setIgnoreMouseEvents(true);   // 鼠标点击穿透：提示只是"看"的，不挡操作
  altHintWin.loadFile(path.join(PET_DIR, "alt-hint.html"));
  altHintWin.webContents.once("did-finish-load", () => {
    altHintReady = true;
    if (altHintPending) { altHintWin.webContents.send("alt-hint:update", altHintPending); altHintPending = null; }
  });
  altHintWin.on("closed", () => { altHintWin = null; altHintReady = false; altHintPending = null; });
}

function altHintSend(payload) {
  if (!altHintWin || altHintWin.isDestroyed()) return;
  if (!altHintReady) { altHintPending = payload; return; }
  altHintWin.webContents.send("alt-hint:update", payload);
}

/**
 * 显示/更新置顶提示横幅。
 * payload: { stage: "prep"|"press"|"release"|"done", sub, remainS, progress }
 *  - prep:    准备播放（提醒先切到微信）
 *  - press:   按住 Alt 说话（红点脉冲 + 大号 Alt）
 *  - release: 松开 Alt 已发送
 *  - done:    收尾
 */
function showAltHint(payload) {
  ensureAltHintWindow();
  if (altHintWin && !altHintWin.isVisible()) altHintWin.showInactive();
  altHintSend(payload);
}

function hideAltHint() {
  disarmCancelKey();
  if (altHintTimer) { clearInterval(altHintTimer); altHintTimer = null; }
  if (altHintWin && !altHintWin.isDestroyed()) altHintWin.hide();
}

/**
 * 彻底销毁提示窗口（主窗关闭 / 应用退出时调用）。
 *
 * 为什么必须有它：hideAltHint 只是 hide()，窗口对象仍然活着。而 Electron 的
 * window-all-closed 要"一个窗口都不剩"才触发，于是那个隐藏的置顶横幅会把整个应用
 * 钉在后台 —— 主窗关了、桌宠也销毁了（右下角看不到任何东西），进程却退不掉，
 * 用户只能开任务管理器杀。横幅本身还可能糊在屏幕上（2026-09-17 用户实测）。
 */
function destroyAltHint() {
  disarmCancelKey();
  if (altHintTimer) { clearInterval(altHintTimer); altHintTimer = null; }
  altHintPending = null;
  try {
    if (altHintWin && !altHintWin.isDestroyed()) altHintWin.destroy();
  } catch {}
  altHintWin = null;
  altHintReady = false;
}

/**
 * 手动变声的持续倒计时引导：按住 Alt 说话、说完松开发送。
 * 从 manualWechatFromPet 拆入本模块（定时器状态 altHintTimer 归此处管理）。
 * @param {number} totalS 引导总秒数
 */
function runManualPressGuide(totalS = 15) {
  armCancelKey();   // 手动录音引导期间允许 Esc 退出
  showAltHint({ stage: "press", sub: "现在按住 <b>Alt</b>，对着麦克风说话，说完松开即发送", remainS: totalS, progress: 0 });
  let t = totalS;
  if (altHintTimer) { clearInterval(altHintTimer); altHintTimer = null; }
  altHintTimer = setInterval(() => {
    t -= 1;
    if (t <= 0) {
      if (altHintTimer) { clearInterval(altHintTimer); altHintTimer = null; }
      showAltHint({ stage: "done", sub: "引导结束，可再按需重发", progress: 1 });
      setTimeout(() => hideAltHint(), 2500);
      return;
    }
    showAltHint({ stage: "press", sub: "按住 <b>Alt</b> 说话，说完松开即发送", remainS: t, progress: 1 - t / totalS });
  }, 1000);
}

// 注：原 runAltHintCountdown（"合成音频要用户按住 Alt 录"的倒计时）已于 2026-09-17 删除。
// 合成语音发送统一走全自动 /api/wechat/send_voice，不再需要人工按 Alt。
// 仍保留 runManualPressGuide —— 那是「真人实时说话走 RVC」的流程，用户本人就是音源，
// 必须自己按 Alt，不属于废弃范围。
module.exports = {
  showAltHint,
  hideAltHint,
  destroyAltHint,
  runManualPressGuide,
};
