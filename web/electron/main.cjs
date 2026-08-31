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

// ---------------- 桌宠窗口（页面导览 + 级联变声状态可视化） ----------------
// 素材：Ice-teapop/desktop-pet 芙宁娜主题 SVG sprite（MIT 许可）
// 显隐策略：默认「常驻显示」，开软件桌宠就在右下角陪着；想它只在变声时出现，
// 右键可切「仅变声时显示」。页面功能介绍在主窗口内显眼处（PetGuide 组件）。
// 位置与开关记忆在 userData/pet.json。
const PET_DIR = path.join(__dirname, "pet");
const PET_PREF_FILE = path.join(app.getPath("userData"), "pet.json");
const PET_PREF_VERSION = 3;   // v3：桌宠默认常驻（v1/v2 默认「仅变声时」，很多人从没见过它）
let petWin = null;
let petPref = { v: PET_PREF_VERSION, mode: "always", guide: true, x: null, y: null }; // always=常驻 | cascade=仅变声时
let petUserHidden = false; // 用户右键隐藏后，等下次级联启动再出现
let petHideTimer = null;
let petGuideUntil = 0;      // 页面导览的展示截止时间戳（ms）
let petLastGuide = null;    // 最近一次导览内容，供右键「再讲一遍本页」复用
let petReady = false;       // pet.html 是否已加载完（加载完成前导览先排队）
let petPendingGuide = null; // 加载完成前收到的导览请求

function loadPetPref() {
  let saved = null;
  try { saved = JSON.parse(fs.readFileSync(PET_PREF_FILE, "utf-8")); } catch {}
  const merged = { ...petPref, ...(saved || {}) };
  // 老配置没有 v 字段（如 {"mode":"cascade","x":..,"y":..}），是「仅变声时显示」时代存的，
  // 升级后一律改为常驻，否则老用户永远看不到桌宠。
  // 坑：Number(undefined) 是 NaN，而 NaN < VERSION 恒为 false，直接比较会让迁移永不生效，
  // 所以必须先判 Number.isFinite。
  const savedV = Number(saved && saved.v);
  if (!saved || !Number.isFinite(savedV) || savedV < PET_PREF_VERSION) {
    merged.v = PET_PREF_VERSION;
    merged.mode = "always";
    merged.x = null;   // 老配置里残留过 y=0 这类越界坐标，会把桌宠顶到屏幕外，一并复位
    merged.y = null;
  }
  return merged;
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

/**
 * 页面导览：前端切页时把「页面介绍 + 动作编排」送到桌宠，
 * 桌宠临时现身在右下角讲完再退场。级联变声运行中不会被导览打断（见 tick 里的优先级）。
 * payload: { page, title, lines[], action, motion, duration }
 */
function showPetGuide(payload) {
  if (!payload || typeof payload !== "object") return;
  if (!petPref.guide) return;          // 用户关掉了导览
  petLastGuide = payload;
  // 桌宠窗口还没建好 / pet.html 还没加载完就先排着，加载完成回调里补发。
  // 主窗口的 React 首屏可能在 petWin 就绪前就发出导览请求，少了这层会漏掉开场介绍。
  if (!petWin || !petReady) { petPendingGuide = payload; return; }
  // 多留 1s 尾巴：让最后一句的字幕停留一下再退场
  petGuideUntil = Date.now() + (Number(payload.duration) || 9000) + 1000;
  if (petHideTimer) { clearTimeout(petHideTimer); petHideTimer = null; }
  petUserHidden = false;
  if (!petWin.isVisible()) petWin.showInactive();
  petWin.webContents.send("pet:guide", payload);
}

function petGuideActive() {
  return Date.now() < petGuideUntil;
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

function petGuideFail(detail) {
  showPetGuide({
    title: "出错了",
    lines: [String(detail || "未知错误").slice(0, 40), "确认后端已启动、微信开着聊天窗口"],
    action: "error", duration: 9000,
  });
}

function petGuideSent(data) {
  // 庆祝动作随机化：每次发成功换种庆祝姿势
  const moves = ["dance", "flip", "bounce", "jump", "twirl", "pop"];
  showPetGuide({
    title: "微信语音",
    lines: [`发出去了！（${data.duration_s || "?"}秒语音）`, "去微信看看吧"],
    action: "play", motion: moves[Math.floor(Math.random() * moves.length)], duration: 9000,
  });
}

/**
 * 发送微信语音消息的公共尾部：调 /api/wechat/send_voice（切麦克风→CABLE Output、
 * 模拟微信官方语音输入、播放、Enter 发送、还原声卡）。执行期间用户别动键鼠/微信。
 */
function sendWechatWav(wavName) {
  backendPost("/api/wechat/send_voice", { wav: wavName }, (data, code) => {
    if (data.ok) { petGuideSent(data); } else { petGuideFail(data.error || `HTTP ${code}`); }
  });
}

/** 桌宠「发送微信语音」：把 outputs/ 下最近一次 TTS 合成发出去。 */
function sendWechatVoiceFromPet() {
  if (!petWin) return;
  showPetGuide({
    title: "微信语音",
    lines: ["我把最近的合成语音录进微信～", "这几秒别动鼠标和微信窗口"],
    action: "think", motion: "work", duration: 8000,
  });
  sendWechatWav(null);
}

/** 桌宠快捷面板：输入文字 → 先 TTS 合成（指定音色，空则用当前选中）→ 再录进微信。 */
function sendWechatTextFromPet(text, voiceId) {
  if (!petWin || !text) return;
  showPetGuide({
    title: "微信语音",
    lines: [`合成中：「${text.slice(0, 12)}${text.length > 12 ? "…" : ""}」`],
    action: "think", motion: "work", duration: 6000,
  });
  backendPost("/api/tts", { text, text_language: "zh", voice_id: voiceId || "" }, (data, code) => {
    if (!data.ok || !data.url) {
      petGuideFail((data.detail && String(data.detail).replace(/^.*detail="?/i, "")) || `TTS HTTP ${code}`);
      return;
    }
    const wav = String(data.url).split("/").pop();
    showPetGuide({
      title: "微信语音",
      lines: [`合成好了（${data.duration_s || "?"}秒），录进微信…`, "别动鼠标和微信窗口"],
      action: "think", motion: "work", duration: 8000,
    });
    sendWechatWav(wav);
  });
}

/** 桌宠快捷面板：实时变声开关（运行中→停止；否则启动，模型用当前实验）。 */
function toggleLiveFromPet() {
  http.get(
    { host: "127.0.0.1", port: BACKEND_PORT, path: "/api/rvc/live/status", timeout: 4000 },
    (res) => {
      let data = "";
      res.on("data", (c) => { data += c; });
      res.on("end", () => {
        let st = {};
        try { st = JSON.parse(data); } catch {}
        if (st.live_running) {
          backendPost("/api/rvc/live/stop", {}, (d, code) => {
            showPetGuide({
              title: "实时变声",
              lines: [d.ok ? "已停止变声" : `停止失败：${(d.detail || code || "").toString().slice(0, 30)}`],
              action: d.ok ? "idle" : "error", duration: 6000,
            });
          });
        } else {
          showPetGuide({
            title: "实时变声",
            lines: ["启动中，模型就绪要一会儿…"],
            action: "build", duration: 6000,
          });
          backendPost("/api/rvc/live/start", {}, (d, code) => {
            if (d.ok) {
              showPetGuide({
                title: "实时变声",
                lines: ["变声已开启！", "微信里把录音设备指向 CABLE Output 就能用"],
                action: "listen", duration: 8000,
              });
            } else {
              petGuideFail(d.detail || `HTTP ${code}`);
            }
          }, 180000);
        }
      });
    },
  ).on("error", () => petGuideFail("后端服务没连上"));
}

/**
 * 设置面板里的「显示桌宠」开关。
 * 打开时顺手把模式切回常驻，否则 cascade 模式会在 5s 后又把它藏起来，
 * 用户会以为开关坏了。
 */
function setPetVisible(v) {
  if (!petWin) return;
  petUserHidden = !v;
  if (v) {
    petPref.mode = "always";
    savePetPref();
    if (petHideTimer) { clearTimeout(petHideTimer); petHideTimer = null; }
    petWin.showInactive();
  } else {
    if (petHideTimer) { clearTimeout(petHideTimer); petHideTimer = null; }
    petGuideUntil = 0;
    petWin.hide();
  }
}

function petVisible() {
  if (!petWin) return false;
  return petPref.mode === "always" ? !petUserHidden : petWin.isVisible();
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

  // pet.html 加载完成后补发排队中的导览（主窗口通常比桌宠先起来，首屏切页会抢跑）
  petWin.webContents.once("did-finish-load", () => {
    petReady = true;
    if (petPendingGuide) {
      const queued = petPendingGuide;
      petPendingGuide = null;
      showPetGuide(queued);
    }
  });
  petWin.on("closed", () => {
    petWin = null;
    petReady = false;
    petGuideUntil = 0;
  });

  // 渲染层（主窗口）切页时请求一次页面导览
  ipcMain.on("pet:guide", (_e, payload) => showPetGuide(payload));

  // 渲染层（设置面板）读取/修改桌宠开关
  ipcMain.handle("pet:prefs", () => ({
    visible: petVisible(),
    guide: petPref.guide !== false,
  }));
  ipcMain.on("pet:visible", (_e, v) => setPetVisible(!!v));
  ipcMain.on("pet:guide-enabled", (_e, v) => { petPref.guide = !!v; savePetPref(); });

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

  // 快捷面板：单击角色发最近合成 / 文字合成后发送 / 实时变声开关
  ipcMain.on("pet:send-last", () => sendWechatVoiceFromPet());
  ipcMain.on("pet:send-text", (_e, text, voiceId) => sendWechatTextFromPet(String(text || "").trim(), String(voiceId || "")));
  ipcMain.on("pet:send-wav", (_e, wav) => sendWechatWav(String(wav || "")));   // 历史记录重发
  ipcMain.on("pet:live-toggle", () => toggleLiveFromPet());

  // 右键菜单：显隐策略 + 页面导览 + 隐藏
  petWin.webContents.on("context-menu", () => {
    if (!petWin) return;
    Menu.buildFromTemplate([
      { label: "发送微信语音（用最近合成）",
        click() { sendWechatVoiceFromPet(); } },
      { type: "separator" },
      { label: "常驻显示", type: "radio", checked: petPref.mode === "always",
        click() { petPref.mode = "always"; savePetPref(); } },
      { label: "仅变声时显示", type: "radio", checked: petPref.mode === "cascade",
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

  // 常驻模式：窗口一就绪就出现在右下角，不用等 2s 轮询的首帧
  petWin.once("ready-to-show", () => {
    if (petWin && !petUserHidden && petPref.mode === "always") petWin.showInactive();
  });

  // 显隐轮询：级联变声或实时变声运行即出现（showInactive 不抢通话焦点），都停止 5s 后隐藏。
  // 页面导览同样会临时唤醒窗口（petGuideActive），讲完自动退场。
  setInterval(async () => {
    if (!petWin) return;
    const [cascade, liveVoice] = await Promise.all([cascadeRunning(), liveVoiceRunning()]);
    const running = cascade || liveVoice;
    if (running) petUserHidden = false;
    const shouldShow = !petUserHidden && (petPref.mode === "always" || running || petGuideActive());
    // 只要满足显示条件就取消未触发的隐藏定时器：停止后 5s 内热键重启级联时
    // 窗口仍可见，走不进 showInactive 分支，旧代码会让定时器到点误藏正在运行的桌宠
    if (shouldShow && petHideTimer) { clearTimeout(petHideTimer); petHideTimer = null; }
    if (shouldShow && !petWin.isVisible()) {
      petWin.showInactive();
    } else if (!shouldShow && petWin.isVisible() && !petHideTimer) {
      petHideTimer = setTimeout(() => {
        petHideTimer = null;
        if (petWin && !petUserHidden && petPref.mode === "cascade" && !petGuideActive()) petWin.hide();
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
      // 主窗口 preload：渲染层借此真正拉起/停止本地后端（见 preload.cjs）
      preload: path.join(__dirname, "preload.cjs"),
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
  registerBackendIpc();
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
