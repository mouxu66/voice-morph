// 后端进程生命周期 + 主进程↔后端 HTTP 工具 + 故障提示/环境体检 + backend IPC
// 从 main.cjs 拆出（2026-09-03 重构），行为与原单文件版本保持一致。
const { app, dialog, ipcMain, shell } = require("electron");
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

function setProjectRoot(root) {
  projectRoot = root;
}
function getProjectRoot() {
  return projectRoot;
}

function resolveProjectRoot() {
  // 优先从本文件位置推导项目根（开发时 __dirname=web/electron，上两级即项目根），
  // 迁移后自动跟随，不再硬编码盘符。
  // 打包安装后后端代码位于 resources/backend/m2_server（见 package.json 的 extraResources），
  // 因此 resources/backend 也要作为候选根。
  const res = process.resourcesPath || "";
  const candidates = [
    path.join(__dirname, "..", ".."),
    // 本机开发根优先：模型权重（4.9G，打包不带）与新代码都在这，打包壳直接复用；
    // 分发机上该目录不存在，自动落到 resources/backend（extraResources 的裸代码）。
    "D:\\变声",
    path.join(res, "backend"),
    path.join(res, "app"),
    path.join(app.getPath("userData"), "project"),
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
    buttons: ["打开日志", "运行环境体检", "关闭"],
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
    void runDoctorDialog();
  }
}

/**
 * 运行环境体检：直接调 tools/doctor.py --json（与本应用共用同一解释器候选），
 * 把逐项检查结果弹给用户看，✗ 项附修复命令 —— 别人装完不用开终端也能自检。
 */
async function runDoctorDialog() {
  const doctorPy = path.join(projectRoot || "", "tools", "doctor.py");
  if (!fs.existsSync(doctorPy)) {
    dialog.showMessageBoxSync({
      type: "info", title: "环境体检", message: `未找到 ${doctorPy}，跳过体检。`,
    });
    return;
  }
  const python = [
    path.join(projectRoot || "", ".venv", "Scripts", "python.exe"),
    "D:\\变声\\.venv\\Scripts\\python.exe",
    "python",
  ].find((p) => p === "python" || fs.existsSync(p));
  let detail = "";
  try {
    const out = execFileSync(python, [doctorPy, "--json"], {
      encoding: "utf-8", timeout: 150000, windowsHide: true,
      cwd: projectRoot || undefined, maxBuffer: 4 * 1024 * 1024,
    });
    const j = JSON.parse(out);
    const rows = (j.checks || []).map((c) =>
      `${c.ok ? "✓" : "✗"} ${c.label}${c.ok ? "" : `\n      ${c.fix || c.detail || "缺依赖"}`}`);
    detail = `${j.ok ? "全部就绪 ✓" : "有必需项未通过，按下面修复后重启应用："}\n\n${rows.join("\n")}`;
  } catch (err) {
    detail = `体检脚本执行失败：${err.message}`;
  }
  dialog.showMessageBoxSync({
    type: "info",
    title: "环境体检结果",
    message: "逐项检查本地推理依赖（Python / torch / 模型 / VB-CABLE 等），✗ 项按提示修复。",
    detail,
    buttons: ["知道了"],
  });
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

// ---------------- 渲染层控制后端（环境体检面板的「启动/停止」按钮） ----------------
// 前端通过 preload 暴露的 window.electron 调用；非桌面端（网页/Vite/局域网）无此桥，
// 前端会降级为「显示启动命令 + 复制」。
function registerBackendIpc() {
  ipcMain.handle("backend:start", async () => {
    if (await backendHealthy()) {
      return { attempted: false, running: true, reusedExternal: true, reason: "" };
    }
    const info = await startBackend(projectRoot);
    if (info && info.attempted) {
      const ok = await waitForBackend(60000);
      return {
        attempted: true,
        running: ok,
        python: info.python,
        reason: ok ? "" : (info.reason || "后端启动超时（60s），请查看日志"),
      };
    }
    return {
      attempted: false,
      running: false,
      reusedExternal: info ? info.reusedExternal : false,
      reason: (info && info.reason) || "未能启动后端",
    };
  });

  ipcMain.handle("backend:stop", async () => {
    stopBackend();
    return { ok: true };
  });

  ipcMain.handle("backend:status", async () => ({
    running: await backendHealthy(),
    port: BACKEND_PORT,
  }));

  ipcMain.handle("backend:show-log", async () => {
    if (!fs.existsSync(BACKEND_LOG)) {
      try { fs.writeFileSync(BACKEND_LOG, "", "utf-8"); } catch {}
    }
    try { shell.showItemInFolder(BACKEND_LOG); } catch {}
    return { ok: true };
  });
}

/** 后端 POST 通用封装：拿到解析后的 JSON（解析失败给空对象）+ 状态码 + 原文。 */
function backendPost(pathname, payload, cb, timeoutMs = 120000) {
  const body = JSON.stringify(payload || {});
  const req = http.request(
    {
      host: "127.0.0.1", port: BACKEND_PORT, path: pathname, method: "POST",
      timeout: timeoutMs,
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
    },
    (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end", () => {
        let json = {};
        try { json = JSON.parse(data); } catch {}
        cb(json, res.statusCode, data);
      });
    },
  );
  req.on("error", (err) => cb({}, 0, String(err)));
  req.write(body);
  req.end();
}

/** 后端 GET/POST 通用封装（Promise 风格）：返回 { code, json }；失败/超时返回 null。 */
function httpJson(method, apiPath, body) {
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

module.exports = {
  BACKEND_PORT,
  setProjectRoot,
  getProjectRoot,
  resolveProjectRoot,
  backendHealthy,
  waitForBackend,
  portInUse,
  startBackend,
  stopBackend,
  registerBackendIpc,
  reportBackendTrouble,
  backendPost,
  httpJson,
};
