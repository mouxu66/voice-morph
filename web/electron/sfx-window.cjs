// 悬浮特效声板窗（**非激活**置顶窗）—— 2026-09-25 新增。
// 设计稿：docs/特效声板设计.md §五之「桌面特效窗口」。
//
// 为什么必须是独立窗口、而且必须是「非激活」窗
// --------------------------------------------
// 用户场景是「微信里按住 Alt 说着话，手去点一声爆炸」。现有四个声板挂点都做不到：
//   · 主窗是普通可聚焦窗 → 点它必然把前台从微信抢走；
//   · 桌宠窗口也不是非激活窗（pet.cjs 只做了 setIgnoreMouseEvents 穿透，
//     因为面板里要能打字，必须可聚焦）→ 点它等价于点主窗；
//   · alt-hint 的置顶横幅**是** focusable:false，但它全程点击穿透（纯装饰）。
// 所以只有 `focusable:false`（= Win32 WS_EX_NOACTIVATE）+ `showInactive()`
// 能让「点得到」与「不改前台窗口」同时成立。**绝不能改成 show()** —— show 会尝试激活。
//
// ⚠️ 这条性质必须用 **SendInput 级真实鼠标**验收（见验收清单 §K A1）：
// CDP 的 Input.dispatchMouseEvent 走渲染进程自己的输入管线，测不到 OS 的激活路径，
// 用它点成功了不能证明真人点不抢焦点。
//
// 声音怎么进微信与这个窗口**无关**：音效由 board_worker 播进 CABLE Input，
// 与微信录的 CABLE Output 在共享模式下自动混音（设计稿 §二）。窗口只是触发器。
const { app, BrowserWindow, screen, ipcMain, globalShortcut } = require("electron");
const path = require("path");
const fs = require("fs");
const backend = require("./backend.cjs");
const { httpJson } = backend;

// 加载渲染层资源：与 pet.cjs / alt-hint.cjs 同一套回退规则
// （安装版 resolveProjectRoot() = resources/backend，磁盘副本优先、asar 内置兜底）。
const _projectPetDir = path.join(backend.resolveProjectRoot(), "web", "electron", "pet");
const PET_DIR = fs.existsSync(path.join(_projectPetDir, "sfx-window.html"))
  ? _projectPetDir
  : path.join(__dirname, "pet");

/** 窗口尺寸（固定）：2 行 × 6 列足够放下当前最多的 12 条素材；再多则窗内横向滚动。 */
const WINDOW_SIZE = { width: 386, height: 158 };
/** 桌宠窗口的标准几何（pet.cjs 建窗时用的同一组数字）—— 默认把声板窗摆在它正上方。 */
const PET_WINDOW_HEIGHT = 480;
const EDGE_GAP = 24;        // 与屏幕右边距（pet.cjs 用 24）
const ABOVE_PET_GAP = 10;   // 与桌宠顶部的间距
/**
 * 全局热键（唤出/收起）。
 * ⚠️ **不许含单 Alt**：微信 PC 的「按住说话」就是 Alt，占了这个键会让用户说不了话。
 * 带修饰键的组合（Ctrl+Alt+X）没问题 —— 微信认的是「Alt 单独按住」。
 */
const SFX_HOTKEY = "Control+Alt+S";
/** 录音引导结束（done）后多久自动收起。留一点尾巴，让最后一眼还能看到窗。 */
const AUTO_HIDE_MS = 3000;

const PREF_FILE = path.join(app.getPath("userData"), "sfx-window.json");
const PREF_VERSION = 1;

let sfxWin = null;
let sfxReady = false;
let sfxPref = { v: PREF_VERSION, x: null, y: null };
let autoHideTimer = null;
let warmRequested = false;

// --------------------------------------------------------------------- 纯函数
// （可单测的部分放这里，见 tools/test-sfx-window.cjs —— 沙箱里起不了真 Electron GUI）


/**
 * 把窗口位置收进工作区（默认落在**右下角、桌宠正上方**）。
 *
 * 为什么必须有：位置是落盘的用户数据，而显示器可能已经换了（副屏拔掉、分辨率变小）。
 * 直接用旧坐标会把窗口丢到看不见的地方 —— 而这是个不可激活窗，用户点不到、
 * 也没法用常规手段把它拖回来。
 *
 * @param {{x: number|null, y: number|null}} pos 记忆的位置（可为空）
 * @param {{width: number, height: number}} size 窗口尺寸
 * @param {{x: number, y: number, width: number, height: number}} workArea 屏工作区
 */
function clampToWorkArea(pos, size, workArea) {
  const defX = workArea.x + workArea.width - size.width - EDGE_GAP;
  const defY = workArea.y + workArea.height - PET_WINDOW_HEIGHT - ABOVE_PET_GAP - size.height;
  const nx = Number.isFinite(pos && pos.x) ? pos.x : defX;
  const ny = Number.isFinite(pos && pos.y) ? pos.y : defY;
  const maxX = workArea.x + workArea.width - size.width;
  const maxY = workArea.y + workArea.height - size.height;
  return {
    x: Math.round(Math.min(Math.max(nx, workArea.x), Math.max(workArea.x, maxX))),
    y: Math.round(Math.min(Math.max(ny, workArea.y), Math.max(workArea.y, maxY))),
  };
}

/**
 * 热键是否安全（不含**单 Alt**）。
 *
 * 判据：出现 Alt 就必须同时出现另一个修饰键。微信按住说话认的是「Alt 单独按住」，
 * 所以 Ctrl+Alt+X / Shift+Alt+X 都不会与它打架，而裸 Alt 或 Alt+X 会。
 */
function isSafeHotkey(accelerator) {
  const tokens = String(accelerator || "").split("+").map((s) => s.trim()).filter(Boolean);
  const hasAlt = tokens.some((t) => t.toLowerCase() === "alt");
  if (!hasAlt) return true;
  const others = ["control", "ctrl", "cmdorctrl", "commandorcontrol", "shift", "super", "command", "cmd"];
  return tokens.some((t) => others.includes(t.toLowerCase()));
}

/** 把格子点击翻译成后端请求体（渲染层只给 id，不给任何路径）。 */
function playPayload(id) {
  return { id: String(id || "") };
}

function loadPref() {
  let saved = null;
  try { saved = JSON.parse(fs.readFileSync(PREF_FILE, "utf-8")); } catch { /* 首次运行没有文件 */ }
  const v = Number(saved && saved.v);
  if (!saved || !Number.isFinite(v) || v < PREF_VERSION) {
    return { v: PREF_VERSION, x: null, y: null };
  }
  return {
    v: PREF_VERSION,
    x: Number.isFinite(saved.x) ? saved.x : null,
    y: Number.isFinite(saved.y) ? saved.y : null,
  };
}

function savePref() {
  try { fs.writeFileSync(PREF_FILE, JSON.stringify(sfxPref), "utf-8"); } catch { /* 落盘失败不该影响使用 */ }
}

// ------------------------------------------------------------------ 窗口生命周期

function ensureSfxWindow() {
  if (sfxWin && !sfxWin.isDestroyed()) return;
  const { workArea } = screen.getPrimaryDisplay();
  const { x, y } = clampToWorkArea(sfxPref, WINDOW_SIZE, workArea);
  sfxWin = new BrowserWindow({
    width: WINDOW_SIZE.width, height: WINDOW_SIZE.height, x, y,
    transparent: true, frame: false, resizable: false,
    alwaysOnTop: true, skipTaskbar: true, hasShadow: false,
    // ⚠️ 非激活窗的两个关键开关：focusable:false + 不用 show()。
    // 去掉任何一个，点格子就会把前台从微信抢走 → 按住 Alt 的录音有被打断的风险。
    focusable: false, show: false,
    webPreferences: {
      preload: path.join(PET_DIR, "sfx-preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  sfxWin.setAlwaysOnTop(true, "screen-saver");
  // 位置变化即落盘（拖动由渲染层报告，见下面的 sfx:drag-*）
  sfxWin.on("moved", () => {
    if (!sfxWin) return;
    const [px, py] = sfxWin.getPosition();
    sfxPref.x = px; sfxPref.y = py;
    savePref();
  });
  sfxWin.loadFile(path.join(PET_DIR, "sfx-window.html"));
  sfxWin.webContents.once("did-finish-load", () => {
    sfxReady = true;
    // 首帧就把目录拉给渲染层（窗口是 hide/show 复用的，不重载页面）
    void pushCatalog();
  });
  sfxWin.on("closed", () => {
    sfxWin = null;
    sfxReady = false;
  });
}

function cancelAutoHide() {
  if (autoHideTimer) { clearTimeout(autoHideTimer); autoHideTimer = null; }
}

/** 唤起声板窗。**只用 showInactive()** —— 见文件头「为什么必须是非激活窗」。 */
function showSfxWindow() {
  ensureSfxWindow();
  cancelAutoHide();
  if (!sfxWin || sfxWin.isDestroyed()) return;
  if (!sfxWin.isVisible()) sfxWin.showInactive();
  // 计数会变（也可能刚装了音效包）→ 每次唤起都重拉一次目录
  void pushCatalog();
  // 首次唤起时预热常驻播放器：否则第一声要付 2~3s 冷导入，听感上像「点了没反应」
  if (!warmRequested) {
    warmRequested = true;
    void httpJson("POST", "/api/soundboard/warm", {});
  }
}

function hideSfxWindow() {
  cancelAutoHide();
  if (sfxWin && !sfxWin.isDestroyed()) sfxWin.hide();   // hide 而非 destroy：下次唤出是热的
}

function toggleSfxWindow() {
  if (sfxWin && !sfxWin.isDestroyed() && sfxWin.isVisible()) hideSfxWindow();
  else showSfxWindow();
}

/**
 * 彻底销毁（主窗关闭 / 应用退出时调用）。
 *
 * 与 alt-hint 同一条教训：`hide()` 只是隐藏，窗口对象还活着。而 Electron 的
 * `window-all-closed` 要「一个窗口都不剩」才触发 —— 一个隐藏的置顶窗能把整个应用
 * 钉在后台：主窗关了、桌宠也没了，进程却退不掉，用户只能开任务管理器杀。
 */
function destroySfxWindow() {
  cancelAutoHide();
  try {
    if (sfxWin && !sfxWin.isDestroyed()) sfxWin.destroy();
  } catch { /* 退出路径上不要抛 */ }
  sfxWin = null;
  sfxReady = false;
}

/**
 * 跟随录音引导阶段显隐（由 alt-hint.cjs 转发）。
 *
 * 只在 **press**（「按住 Alt 说话」）时浮现：那是用户真会去点格子的时刻。
 * 不跟 `prep` —— 那条是**全自动**发送（程序点话筒、界面明说「别动键鼠」），
 * 摆一个声板窗出来只会诱导用户去点，反而打断发送。
 */
function onRecordingStage(stage) {
  if (stage === "press") {
    showSfxWindow();
  } else if (stage === "done") {
    cancelAutoHide();
    autoHideTimer = setTimeout(() => { autoHideTimer = null; hideSfxWindow(); }, AUTO_HIDE_MS);
  }
}

// ------------------------------------------------------------------------- IPC

/** 把目录推给渲染层。`sfx:refresh` 的负载必须是**已求值**的对象 —— 推一个 Promise 过去
 * 会在结构化克隆那步变成 `{}`（渲染层收到的格子就永远是空的，且不报错）。 */
async function pushCatalog() {
  const payload = await listCatalog();
  if (sfxWin && !sfxWin.isDestroyed() && sfxReady) sfxWin.webContents.send("sfx:refresh", payload);
}

async function listCatalog() {
  const r = await httpJson("GET", "/api/soundboard/catalog");
  if (!r) return { ok: false, items: [], error: "后端没响应（应用可能在启动中）" };
  if (r.code !== 200 || !r.json || r.json.ok === false) {
    return { ok: false, items: [], error: (r.json && (r.json.detail || r.json.error)) || `HTTP ${r.code}` };
  }
  return { ok: true, items: r.json.items || [], error: "" };
}

/**
 * 注册 IPC 与热键（主进程装配层调用一次）。
 *
 * 渲染层**不直连后端**（抄桌宠那条路）：它只报 id，路径解析、错误翻译都在主进程，
 * 于是「素材目录的绝对路径」永远不出现在渲染层。
 */
function registerSfxIpc() {
  ipcMain.handle("sfx:list", () => listCatalog());
  ipcMain.handle("sfx:play", async (_e, id) => {
    const r = await httpJson("POST", "/api/soundboard/play", playPayload(id));
    if (!r) return { ok: false, error: "后端没响应" };
    if (r.code === 200 && r.json && r.json.ok !== false) {
      return { ok: true, duration_s: r.json.duration_s || 0 };
    }
    // 503 设备缺失 / 404 素材没了 —— 原样带上去，让格子上出红边并写出原因。
    // 静默失败是最不能接受的（与预混那两条判据同源）。
    return { ok: false, error: (r.json && (r.json.detail || r.json.error)) || `HTTP ${r.code}` };
  });
  ipcMain.handle("sfx:stop", async () => {
    const r = await httpJson("POST", "/api/soundboard/stop", {});
    return { ok: !!r && r.code === 200 };
  });
  ipcMain.on("sfx:hide", () => hideSfxWindow());
  ipcMain.on("sfx:toggle", () => toggleSfxWindow());
  // 诊断面：也用于**真机验收**（§K A1）——「点完没抢焦点」这条必须能当场读出来，
  // 不能靠「我觉得微信还在前台」。bounds 是 DIP，与渲染层 CSS px 同一坐标系。
  ipcMain.handle("sfx:info", () => {
    const alive = !!(sfxWin && !sfxWin.isDestroyed());
    return {
      hotkey: SFX_HOTKEY,
      visible: !!(alive && sfxWin.isVisible()),
      focused: !!(alive && sfxWin.isFocused()),
      bounds: alive ? sfxWin.getBounds() : null,
    };
  });

  // 拖拽：app-region 在置顶透明窗上不可靠（pet.cjs 已踩过），改由渲染层报告、
  // 主进程按光标屏幕坐标挪窗口。**不激活窗口**这一点在拖拽期间同样成立（focusable:false）。
  let dragOrigin = null;
  ipcMain.on("sfx:drag-start", () => {
    if (!sfxWin || sfxWin.isDestroyed()) return;
    const cur = screen.getCursorScreenPoint();
    const b = sfxWin.getBounds();
    dragOrigin = { dx: cur.x - b.x, dy: cur.y - b.y };
  });
  ipcMain.on("sfx:drag-move", () => {
    if (!dragOrigin || !sfxWin || sfxWin.isDestroyed()) return;
    const cur = screen.getCursorScreenPoint();
    sfxWin.setPosition(cur.x - dragOrigin.dx, cur.y - dragOrigin.dy);
  });
  ipcMain.on("sfx:drag-end", () => {
    if (dragOrigin && sfxWin && !sfxWin.isDestroyed()) {
      const [px, py] = sfxWin.getPosition();
      sfxPref.x = px; sfxPref.y = py;
      savePref();
    }
    dragOrigin = null;
  });

  sfxPref = loadPref();
  if (!isSafeHotkey(SFX_HOTKEY)) {
    console.error(`[sfx] 热键 ${SFX_HOTKEY} 含单 Alt，与微信按住说话冲突 —— 拒绝注册`);
    return;
  }
  const ok = globalShortcut.register(SFX_HOTKEY, () => toggleSfxWindow());
  if (!ok) console.error(`[sfx] 热键 ${SFX_HOTKEY} 注册失败（可能被其它程序占用）`);
}

module.exports = {
  // 生命周期
  showSfxWindow,
  hideSfxWindow,
  toggleSfxWindow,
  destroySfxWindow,
  onRecordingStage,
  registerSfxIpc,
  // 纯函数与常量（单测用；也供文档引用）
  clampToWorkArea,
  isSafeHotkey,
  playPayload,
  SFX_HOTKEY,
  WINDOW_SIZE,
  AUTO_HIDE_MS,
};
