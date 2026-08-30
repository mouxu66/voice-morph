// Electron 主进程：启动桌面壳 + 拉起 Python 推理后端
const { app, BrowserWindow, Menu, dialog, ipcMain, screen, shell, globalShortcut } = require("electron");
const { spawn, execFileSync } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

const BACKEND_PORT = 8000;
let backendProc = null;
let projectRoot = null;
const PID_FILE = path.join(app.getPath("userData"), "backend.pid");
// 后端 stdout/stderr 落盘：装到别人机器上出问题时，这是唯一能自查的线索
const BACKEND_LOG = path.join(app.getPath("userData"), "backend.log");

function resolveProjectRoot() {
  // 优先从本文件位置推导项目根（开发时 __dirname=web/electron，上两级即项目根），
  // 迁移后自动跟随，不再硬编码盘符。
  // 打包安装后后端代码位于 resources/backend/m2_server（见 package.json 的 extraResources），
  // 因此 resources/backend 也要作为候选根。
  const res = process.resourcesPath || "";
  const candidates = [
    path.join(__dirname, "..", ".."),
    path.join(res, "backend"),
    path.join(res, "app"),
    path.join(app.getPath("userData"), "project"),
    "D:\\变声",
  ];
  for (const c of candidates) {
    if (c && fs.existsSync(path.join(c, "m2_server", "server.py"))) return c;
  }
  return path.join(__dirname, "..", "..");
}

/** 探测后端 /api/health 是否可用 */
function backendHealthy() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/health", timeout: 2000 },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForBackend(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await backendHealthy()) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return backendHealthy();
}

/**
 * 后端起不来时给出可操作的提示。
 * 没有这个提示，别人装完只能看到一个漂亮但什么都不干的前端 —— 这是本应用最容易踩的坑。
 */
async function reportBackendTrouble(info) {
  if (await waitForBackend(45000)) return;
  const setupPs1 = path.join(projectRoot || "", "tools", "setup_env.ps1");
  const lines = [
    `后端代码位置：${projectRoot}`,
    info.python ? `使用的解释器：${info.python}` : "未找到可用的 Python 解释器",
    info.reason ? `原因：${info.reason}` : "",
    "",
    "本应用的前端只是界面，所有推理都在本地 Python 后端里完成。",
    "请先准备一次运行环境（只需一次）：",
    setupPs1 && fs.existsSync(setupPs1)
      ? `  1. 右键以 PowerShell 运行：${setupPs1}`
      : "  1. 在项目目录执行：python -m venv .venv 并 pip install -r requirements.txt",
    "  2. 或手动运行 tools\\doctor.py 查看缺什么",
    "",
    `后端日志：${BACKEND_LOG}`,
  ].filter(Boolean);

  const choice = dialog.showMessageBoxSync({
    type: "warning",
    title: "本地推理服务未启动",
    message: "推理后端没有启动，界面能打开但功能不可用。",
    detail: lines.join("\n"),
    buttons: ["打开日志", "查看环境体检步骤", "关闭"],
    defaultId: 1,
    cancelId: 2,
  });
  if (choice === 0) {
    if (!fs.existsSync(BACKEND_LOG)) {
      try {
        fs.writeFileSync(BACKEND_LOG, "", "utf-8");
      } catch {}
    }
    shell.showItemInFolder(BACKEND_LOG);
  } else if (choice === 1) {
    const toolsDir = path.join(projectRoot || "", "tools");
    if (fs.existsSync(toolsDir)) shell.openPath(toolsDir);
  }
}

function portInUse(port) {
  const net = require("net");
  return new Promise((resolve) => {
    const s = net.createConnection({ port, host: "127.0.0.1" });
    s.on("connect", () => { s.destroy(); resolve(true); });
    s.on("error", () => resolve(false));
  });
}

// 判断端口占用进程是否由本应用（python.exe server.py）拉起；是则返回其 pid
function getBackendPidOnPort(port) {
  try {
    const out = execFileSync(
      "powershell",
      ["-NoProfile", "-Command",
        `(Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue).OwningProcess -join ','`],
      { windowsHide: true, timeout: 8000 },
    ).toString().trim();
    return out ? out.split(",").map((x) => parseInt(x, 10)) : [];
  } catch {
    return [];
  }
}

function isOurBackend(pid) {
  try {
    const py = require("child_process").execFileSync(
      "powershell",
      ["-NoProfile", "-Command",
        `(Get-CimInstance Win32_Process -Filter "ProcessId=${pid}").CommandLine`],
      { windowsHide: true, timeout: 8000 },
    ).toString().trim();
    return /server\.py/.test(py);
  } catch {
    return false;
  }
}

function saveBackendPid() {
  try { fs.writeFileSync(PID_FILE, String(backendProc ? backendProc.pid : ""), "utf-8"); } catch {}
}
function readSavedPid() {
  try { return parseInt(fs.readFileSync(PID_FILE, "utf-8").trim(), 10) || null; } catch { return null; }
}

// 强制结束一个进程及其子进程（Windows 用 taskkill /T）
function killProcessTree(pid) {
  if (!pid) return;
  try {
    execFileSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true, timeout: 10000, stdio: "ignore" });
  } catch {}
}

/**
 * 数据目录：安装版后端代码位于 resources 下（只读、随版本覆盖），
 * media/outputs 等用户数据必须放到可写位置。
 * 优先级：开发目录自带 media > 历史数据目录 D:\变声 > userData。
 * 通过 VM_MEDIA_DIR / VM_OUTPUTS_DIR 传给后端（m2_server/config.py 已支持）。
 */
const LEGACY_ROOT = "D:\\变声";

function resolveDataRoot(root) {
  if (fs.existsSync(path.join(root, "media"))) return root;
  if (fs.existsSync(path.join(LEGACY_ROOT, "media"))) return LEGACY_ROOT;
  return app.getPath("userData");
}

async function startBackend(root) {
  // python 解释器：优先安装目录自带 .venv（开发态），安装版回退到项目目录 D:\变声\.venv（依赖齐全），
  // 都没有才用系统 python（依赖可能缺失，仅兜底）
  const candidates = [
    path.join(root, ".venv", "Scripts", "python.exe"),
    path.join(LEGACY_ROOT, ".venv", "Scripts", "python.exe"),
  ];
  const found = candidates.find((p) => fs.existsSync(p));
  const python = found ?? "python";
  const serverPy = path.join(root, "m2_server", "server.py");
  if (!fs.existsSync(serverPy)) {
    return { attempted: false, python: null, reason: `未找到后端代码：${serverPy}` };
  }

  // 解除端口限制：端口被占时，若占用者是我们自己的后端（含上次残留/僵尸），一律清掉再拉起，
  // 避免僵尸进程挡路导致“无法自动拉起”。非本应用拉起的进程（用户手动）才复用。
  if (await portInUse(BACKEND_PORT)) {
    const pids = getBackendPidOnPort(BACKEND_PORT);
    const ours = pids.filter((p) => isOurBackend(p));
    if (ours.length) {
      console.log(`[backend] 端口 ${BACKEND_PORT} 被本应用残留后端占用，清理后重新拉起: ${ours.join(",")}`);
      ours.forEach(killProcessTree);
      await new Promise((r) => setTimeout(r, 1500)); // 等端口释放
    } else {
      console.log(`[backend] 端口 ${BACKEND_PORT} 被外部进程占用，复用之`);
      return { attempted: false, reusedExternal: true, python: null, reason: "" };
    }
  }

  // 每次启动重写日志，避免旧日志误导排查
  try {
    fs.writeFileSync(
      BACKEND_LOG,
      `[launch] ${new Date().toISOString()} root=${root} python=${python}\n`,
      "utf-8",
    );
  } catch {}
  const logStream = fs.createWriteStream(BACKEND_LOG, { flags: "a" });

  let spawnError = "";
  const dataRoot = resolveDataRoot(root);
  backendProc = spawn(python, [serverPy], {
    cwd: path.join(root, "m2_server"),
    env: {
      ...process.env,
      PYTHONPATH: [
        root,
        process.env.PYTHONPATH,
      ].filter(Boolean).join(path.delimiter),
      PYTHONIOENCODING: "utf-8",
      VM_MEDIA_DIR: path.join(dataRoot, "media"),
      VM_OUTPUTS_DIR: path.join(dataRoot, "outputs"),
      // TTS worker 的 venv312 只存在于项目目录（安装包不含），存在则注入给 qwen3_tts.py
      ...(fs.existsSync(path.join(LEGACY_ROOT, "tts_trial", "venv312"))
        ? { VM_PROJECT_ROOT: LEGACY_ROOT }
        : {}),
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  saveBackendPid();
  const pump = (tag) => (d) => {
    const text = d.toString();
    console.log(`[backend${tag}]`, text.trim());
    try {
      logStream.write(text);
    } catch {}
  };
  backendProc.stdout.on("data", pump(""));
  backendProc.stderr.on("data", pump(":err"));
  backendProc.on("exit", (code) => {
    console.log(`[backend] exited with code ${code}`);
    try {
      logStream.write(`\n[exit] code=${code}\n`);
    } catch {}
    backendProc = null;
  });
  // spawn 失败（如 python 不存在）会触发 error，未监听将导致主进程崩溃
  backendProc.on("error", (err) => {
    spawnError = err.message;
    console.error(`[backend] 启动失败: ${err.message}`);
    console.error(`[backend] 请确认 Python 后端环境就绪（.venv 或系统 python）`);
    try {
      logStream.write(`\n[spawn error] ${err.message}\n`);
    } catch {}
    backendProc = null;
  });
  return { attempted: true, python, reason: spawnError, reusedExternal: false };
}

// 关闭时必须把后端也关掉：优先用记录的 pid 整树结束；再兜底 kill 当前 proc
function stopBackend() {
  if (backendProc) {
    killProcessTree(backendProc.pid);
    try { backendProc.kill("SIGKILL"); } catch {}
    backendProc = null;
  } else {
    const saved = readSavedPid();
    if (saved) killProcessTree(saved);
  }
  try { fs.unlinkSync(PID_FILE); } catch {}
}

// ---------------- 桌宠窗口（级联变声状态可视化） ----------------
// 素材：Ice-teapop/desktop-pet 芙宁娜主题 SVG sprite（MIT 许可）
// 显隐策略：默认「仅级联变声时显示」，级联启动即出现在右下角，停止 5s 后隐藏；
// 右键可切「常驻显示」。位置记忆在 userData/pet.json。
const PET_DIR = path.join(__dirname, "pet");
const PET_PREF_FILE = path.join(app.getPath("userData"), "pet.json");
let petWin = null;
let petPref = { mode: "cascade", x: null, y: null }; // cascade=仅级联时 | always=常驻
let petUserHidden = false; // 用户右键隐藏后，等下次级联启动再出现
let petHideTimer = null;

function loadPetPref() {
  try { return { ...petPref, ...JSON.parse(fs.readFileSync(PET_PREF_FILE, "utf-8")) }; }
  catch { return petPref; }
}
function savePetPref() {
  try { fs.writeFileSync(PET_PREF_FILE, JSON.stringify(petPref), "utf-8"); } catch {}
}

function cascadeRunning() {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/cascade/status", timeout: 2000 },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try { resolve(Boolean(JSON.parse(data).running)); } catch { resolve(false); }
        });
      },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function liveVoiceRunning() {
  // 实时变声（RVC）运行中 → 桌宠也出现（挂实时字幕）
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/rvc/live/status", timeout: 2000 },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try { resolve(Boolean(JSON.parse(data).live_running)); } catch { resolve(false); }
        });
      },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function createPetWindow() {
  petPref = loadPetPref();
  const { workArea } = screen.getPrimaryDisplay();
  const width = 220, height = 300;
  const x = Number.isFinite(petPref.x) ? petPref.x : workArea.x + workArea.width - width - 24;
  const y = Number.isFinite(petPref.y) ? petPref.y : workArea.y + workArea.height - height - 8;
  petWin = new BrowserWindow({
    width, height, x, y,
    transparent: true, frame: false, resizable: false,
    alwaysOnTop: true, skipTaskbar: true, hasShadow: false,
    show: false, // 由显隐轮询决定何时出现，不抢主窗口焦点
    webPreferences: {
      preload: path.join(PET_DIR, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  petWin.setAlwaysOnTop(true, "screen-saver");
  // VM_PET_MOCK=1：调试模式，自动轮播各状态动画（pet.html?mock=1），不依赖后端
  petWin.loadFile(path.join(PET_DIR, "pet.html"),
    process.env.VM_PET_MOCK === "1" ? { query: { mock: "1" } } : undefined);
  petWin.webContents.on("render-process-gone", (_e, d) =>
    console.error(`[pet] renderer gone: ${d?.reason}`));

  // 记忆拖动后的位置（手动拖拽结束/窗口被挪动时触发）
  petWin.on("moved", () => {
    if (!petWin) return;
    const [px, py] = petWin.getPosition();
    petPref.x = px; petPref.y = py;
    savePetPref();
  });

  // 手动拖拽（app-region 在透明+穿透窗口上不可靠，改由渲染器报告、主进程挪窗口）
  let dragOrigin = null;
  ipcMain.on("pet:drag-start", () => {
    if (!petWin) return;
    const cur = screen.getCursorScreenPoint();
    const b = petWin.getBounds();
    dragOrigin = { dx: cur.x - b.x, dy: cur.y - b.y };
  });
  ipcMain.on("pet:drag-move", () => {
    if (!dragOrigin || !petWin) return;
    const cur = screen.getCursorScreenPoint();
    petWin.setPosition(cur.x - dragOrigin.dx, cur.y - dragOrigin.dy);
  });
  ipcMain.on("pet:drag-end", () => {
    if (dragOrigin && petWin) {
      const [px, py] = petWin.getPosition();
      petPref.x = px; petPref.y = py;
      savePetPref();
    }
    dragOrigin = null;
  });

  // 渲染器只在悬停角色/气泡时才请求可交互，其余区域点击穿透
  ipcMain.on("pet:ignore-mouse", (e, v) => {
    if (petWin && e.sender === petWin.webContents) {
      petWin.setIgnoreMouseEvents(v, { forward: true });
    }
  });

  // 右键菜单：显隐策略 + 隐藏
  petWin.webContents.on("context-menu", () => {
    if (!petWin) return;
    Menu.buildFromTemplate([
      { label: "常驻显示", type: "radio", checked: petPref.mode === "always",
        click() { petPref.mode = "always"; savePetPref(); } },
      { label: "仅级联变声时显示", type: "radio", checked: petPref.mode === "cascade",
        click() { petPref.mode = "cascade"; savePetPref(); } },
      { type: "separator" },
      { label: "隐藏桌宠",
        click() { petUserHidden = true; if (petWin) petWin.hide(); } },
    ]).popup({ window: petWin });
  });

  // 调试模式直接显示，不参与显隐轮询
  if (process.env.VM_PET_MOCK === "1") {
    petWin.once("ready-to-show", () => petWin.showInactive());
    return;
  }

  // 显隐轮询：级联变声或实时变声运行即出现（showInactive 不抢通话焦点），都停止 5s 后隐藏
  setInterval(async () => {
    if (!petWin) return;
    const [cascade, liveVoice] = await Promise.all([cascadeRunning(), liveVoiceRunning()]);
    const running = cascade || liveVoice;
    if (running) petUserHidden = false;
    const shouldShow = !petUserHidden && (petPref.mode === "always" || running);
    // 只要满足显示条件就取消未触发的隐藏定时器：停止后 5s 内热键重启级联时
    // 窗口仍可见，走不进 showInactive 分支，旧代码会让定时器到点误藏正在运行的桌宠
    if (shouldShow && petHideTimer) { clearTimeout(petHideTimer); petHideTimer = null; }
    if (shouldShow && !petWin.isVisible()) {
      petWin.showInactive();
    } else if (!shouldShow && petWin.isVisible() && !petHideTimer) {
      petHideTimer = setTimeout(() => {
        petHideTimer = null;
        if (petWin && !petUserHidden && petPref.mode === "cascade") petWin.hide();
      }, 5000);
    }
  }, 2000);
}

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
      // 本地应用加载本地后端：file:// 页面需要访问 http://127.0.0.1，关闭 webSecurity 避免 fetch 被拦
      webSecurity: false,
      allowRunningInsecureContent: true,
    },
  });

  // 主窗口关闭即退出（桌宠不独立驻留）：销毁桌宠让 window-all-closed 生效
  win.on("closed", () => {
    if (petWin) { petWin.destroy(); petWin = null; }
  });

  const distHtml = path.join(__dirname, "..", "dist", "index.html");
  const projectDistHtml = path.join(root, "web", "dist", "index.html");
  // 默认永远加载生产构建（项目内最新 dist 优先，其次 asar 内置产物）。
  // 仅当显式设置 ELECTRON_IS_DEV=1 且 5173 在跑时，才加载 Vite 开发服务器（调试用）。
  // 这样可避免「遗留的 dev server 偷偷接管打包应用导致离线」的陷阱。
  if (process.env.ELECTRON_IS_DEV === "1" && (await portInUse(5173))) {
    win.loadURL("http://localhost:5173");
  } else if (fs.existsSync(projectDistHtml)) {
    win.loadFile(projectDistHtml);
  } else if (fs.existsSync(distHtml)) {
    win.loadFile(distHtml);
  } else {
    console.error("[frontend] 未找到构建产物 dist/index.html");
  }
}

app.whenReady().then(async () => {
  const root = resolveProjectRoot();
  projectRoot = root;
  // VM_PET_MOCK=1：桌宠单独调试模式，不拉后端不开主窗口，动画自动轮播
  if (process.env.VM_PET_MOCK === "1") {
    createPetWindow();
    return;
  }
  const startInfo = await startBackend(root);
  createWindow(root);
  createPetWindow();
  registerCascadeHotkey();
  // 后端探测放在窗口之后异步进行，不阻塞界面出现；探不到才弹提示
  void reportBackendTrouble(startInfo || {});

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow(root);
  });
});

/** 全局热键 Ctrl+Alt+V：一键启停级联变声（游戏全屏/微信通话中无需切回应用）。
 *  toggle 逻辑跑在主进程，直接 HTTP 调本机后端：
 *  running -> POST stop；否则取上次启动参数 POST start（无存档则默认音色）。
 *  前端状态页本来就 1s 轮询 status，热键启停后界面自动跟上，无需额外通知。
 */
function _httpJson(method, apiPath, body) {
  return new Promise((resolve) => {
    const data = body ? JSON.stringify(body) : null;
    const req = http.request(
      {
        host: "127.0.0.1", port: BACKEND_PORT, path: apiPath, method,
        timeout: 8000,
        headers: data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {},
      },
      (res) => {
        let buf = "";
        res.setEncoding("utf8");
        res.on("data", (c) => { buf += c; });
        res.on("end", () => {
          try { resolve({ code: res.statusCode, json: JSON.parse(buf) }); }
          catch { resolve({ code: res.statusCode, json: null }); }
        });
      },
    );
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
    if (data) req.write(data);
    req.end();
  });
}

async function toggleCascadeHotkey() {
  const st = await _httpJson("GET", "/api/cascade/status");
  if (!st) return; // 后端不在，热键静默（应用界面有状态指示）
  if (st.json && st.json.running) {
    await _httpJson("POST", "/api/cascade/stop");
    return;
  }
  // 复用上次启动参数（音色/分块等），没有存档就带空 body（后端用默认音色）
  const last = await _httpJson("GET", "/api/cascade/last-start");
  const body = last && last.json ? last.json : {};
  await _httpJson("POST", "/api/cascade/start", body);
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
    _restoreAudioOnExit(projectRoot);
  } catch {
    /* 忽略 */
  }
  globalShortcut.unregisterAll(); // 全局热键随应用退出释放
  stopBackend(); // 关闭后端，绝不残留
});

// 关闭最后一个窗口时，连同后端一起退出（打包桌面应用标准行为）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    stopBackend();
    app.quit();
  }
});
