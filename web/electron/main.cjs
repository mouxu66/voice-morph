// Electron 主进程：启动桌面壳 + 拉起 Python 推理后端
const { app, BrowserWindow } = require("electron");
const { spawn, execFileSync } = require("child_process");
const path = require("path");
const fs = require("fs");

const BACKEND_PORT = 8000;
let backendProc = null;
let projectRoot = null;
const PID_FILE = path.join(app.getPath("userData"), "backend.pid");

function resolveProjectRoot() {
  // 优先从本文件位置推导项目根（开发时 __dirname=web/electron，上两级即项目根），
  // 迁移后自动跟随，不再硬编码盘符。依次兜底 resources/app、用户数据目录，
  // 最后回退到历史默认 D:\变声。
  const candidates = [
    path.join(__dirname, "..", ".."),
    path.join(process.resourcesPath, "app"),
    path.join(app.getPath("userData"), "project"),
    "D:\\变声",
  ];
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, "m2_server", "server.py"))) return c;
  }
  return path.join(__dirname, "..", "..");
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

async function startBackend(root) {
  const venvPython = path.join(root, ".venv", "Scripts", "python.exe");
  const python = fs.existsSync(venvPython) ? venvPython : "python";
  const serverPy = path.join(root, "m2_server", "server.py");
  if (!fs.existsSync(serverPy)) return;

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
      return;
    }
  }

  backendProc = spawn(python, [serverPy], {
    cwd: path.join(root, "m2_server"),
    env: {
      ...process.env,
      PYTHONPATH: [
        root,
        process.env.PYTHONPATH,
      ].filter(Boolean).join(path.delimiter),
      PYTHONIOENCODING: "utf-8",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  saveBackendPid();
  backendProc.stdout.on("data", (d) => console.log("[backend]", d.toString().trim()));
  backendProc.stderr.on("data", (d) => console.log("[backend:err]", d.toString().trim()));
  backendProc.on("exit", (code) => {
    console.log(`[backend] exited with code ${code}`);
    backendProc = null;
  });
  // spawn 失败（如 python 不存在）会触发 error，未监听将导致主进程崩溃
  backendProc.on("error", (err) => {
    console.error(`[backend] 启动失败: ${err.message}`);
    console.error(`[backend] 请确认 Python 后端环境就绪（.venv 或系统 python）`);
    backendProc = null;
  });
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
  await startBackend(root);
  createWindow(root);

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow(root);
  });
});

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
  stopBackend(); // 关闭后端，绝不残留
});

// 关闭最后一个窗口时，连同后端一起退出（打包桌面应用标准行为）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    stopBackend();
    app.quit();
  }
});
