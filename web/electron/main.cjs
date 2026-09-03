// Electron 主进程装配层：启动桌面壳 + 拉起 Python 推理后端。
// 2026-09-03 重构：按职责拆分为 backend.cjs（后端进程/HTTP/故障提示）、
// pet.cjs（桌宠窗口）、pet-actions.cjs（桌宠动作）、alt-hint.cjs（置顶提示）、
// update-ipc.cjs（自动更新 IPC），本文件只保留装配与生命周期。
const { app, BrowserWindow, globalShortcut } = require("electron");
const { execFileSync } = require("child_process");
const path = require("path");
const fs = require("fs");

const backend = require("./backend.cjs");
const pet = require("./pet.cjs");
const petActions = require("./pet-actions.cjs");
const { registerUpdateIpc, scheduleStartupUpdateCheck } = require("./update-ipc.cjs");

async function createWindow(root) {
  const win = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    title: "变声工坊 · Voice Morph Studio",
    backgroundColor: "#0b0e14",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // 主窗口 preload：渲染层借此真正拉起/停止本地后端（见 preload.cjs）
      preload: path.join(__dirname, "preload.cjs"),
      // 本地应用加载本地后端：file:// 页面需要访问 http://127.0.0.1，关闭 webSecurity 避免 fetch 被拦
      webSecurity: false,
      allowRunningInsecureContent: true,
    },
  });

  // 主窗口关闭即退出（桌宠不独立驻留）：销毁桌宠让 window-all-closed 生效
  win.on("closed", () => {
    pet.destroyPet();
  });

  const distHtml = path.join(__dirname, "..", "dist", "index.html");
  const projectDistHtml = path.join(root, "web", "dist", "index.html");
  // 默认永远加载生产构建（项目内最新 dist 优先，其次 asar 内置产物）。
  // 仅当显式设置 ELECTRON_IS_DEV=1 且 5173 在跑时，才加载 Vite 开发服务器（调试用）。
  // 这样可避免「遗留的 dev server 偷偷接管打包应用导致离线」的陷阱。
  if (process.env.ELECTRON_IS_DEV === "1" && (await backend.portInUse(5173))) {
    win.loadURL("http://localhost:5173");
  } else if (fs.existsSync(projectDistHtml)) {
    win.loadFile(projectDistHtml);
  } else if (fs.existsSync(distHtml)) {
    win.loadFile(distHtml);
  } else {
    console.error("[frontend] 未找到构建产物 dist/index.html");
  }
  return win;
}

app.whenReady().then(async () => {
  const root = backend.resolveProjectRoot();
  backend.setProjectRoot(root);
  // VM_PET_MOCK=1：桌宠单独调试模式，不拉后端不开主窗口，动画自动轮播
  if (process.env.VM_PET_MOCK === "1") {
    pet.createPetWindow(petActions);
    return;
  }
  const startInfo = await backend.startBackend(root);
  const win = await createWindow(root);
  pet.createPetWindow(petActions);
  backend.registerBackendIpc();
  registerCascadeHotkey();
  registerUpdateIpc();
  // 启动 12s 后静默检查一次更新，有新版才弹更新页（失败全程静默）
  scheduleStartupUpdateCheck(win);
  // 后端探测放在窗口之后异步进行，不阻塞界面出现；探不到才弹提示
  void backend.reportBackendTrouble(startInfo || {});

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) void createWindow(root);
  });
});

/** 全局热键 Ctrl+Alt+V：一键启停级联变声（游戏全屏/微信通话中无需切回应用）。
 *  toggle 逻辑跑在主进程，直接 HTTP 调本机后端：
 *  running -> POST stop；否则取上次启动参数 POST start（无存档则默认音色）。
 *  前端状态页本来就 1s 轮询 status，热键启停后界面自动跟上，无需额外通知。
 */
async function toggleCascadeHotkey() {
  const st = await backend.httpJson("GET", "/api/cascade/status");
  if (!st) return; // 后端不在，热键静默（应用界面有状态指示）
  if (st.json && st.json.running) {
    await backend.httpJson("POST", "/api/cascade/stop");
    return;
  }
  // 复用上次启动参数（音色/分块等），没有存档就带空 body（后端用默认音色）
  const last = await backend.httpJson("GET", "/api/cascade/last-start");
  const body = last && last.json ? last.json : {};
  await backend.httpJson("POST", "/api/cascade/start", body);
}

function registerCascadeHotkey() {
  const OK = globalShortcut.register("Control+Alt+V", () => {
    void toggleCascadeHotkey();
  });
  if (!OK) console.error("[hotkey] Ctrl+Alt+V 注册失败（可能被其它程序占用）");
}


// 退出软件时把用户原始音频默认设备还原，避免“用完变声后突然没声音”
function _findShell() {
  for (const name of ["pwsh", "powershell"]) {
    try {
      const out = execFileSync("where", [name], { windowsHide: true, timeout: 5000 }).toString().trim();
      const first = out.split(/\r?\n/)[0];
      if (first) return first;
    } catch {
      /* 继续尝试下一个 */
    }
  }
  return null;
}

function _restoreAudioOnExit(root) {
  const ps1 = path.join(root, "m2_server", "audio_config.ps1");
  if (!fs.existsSync(ps1)) return;
  const shell = _findShell();
  if (!shell) return;
  try {
    execFileSync(
      shell,
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-action", "restore"],
      { windowsHide: true, timeout: 20000, stdio: "ignore" },
    );
  } catch {
    /* 还原失败不应阻塞退出 */
  }
}

app.on("before-quit", () => {
  try {
    _restoreAudioOnExit(backend.getProjectRoot());
  } catch {
    /* 忽略 */
  }
  globalShortcut.unregisterAll(); // 全局热键随应用退出释放
  backend.stopBackend(); // 关闭后端，绝不残留
});

// 关闭最后一个窗口时，连同后端一起退出（打包桌面应用标准行为）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    backend.stopBackend();
    app.quit();
  }
});
