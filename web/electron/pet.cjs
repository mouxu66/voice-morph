// 桌宠窗口（页面导览 + 级联变声状态可视化）—— 从 main.cjs 拆出，行为保持一致。
// 素材：Ice-teapop/desktop-pet 芙宁娜主题 SVG sprite（MIT 许可）
// 显隐策略：默认「常驻显示」，开软件桌宠就在右下角陪着；想它只在变声时出现，
// 右键可切「仅变声时显示」。页面功能介绍在主窗口内显眼处（PetGuide 组件）。
// 位置与开关记忆在 userData/pet.json。
//
// 注意：桌宠触发的业务动作（发微信语音/挖掘/实时变声开关）通过 createPetWindow(actions)
// 依赖注入传入（见 pet-actions.cjs），本模块不反向依赖动作模块，避免循环 require。
const { app, BrowserWindow, Menu, ipcMain, screen } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { BACKEND_PORT } = require("./backend.cjs");

const PET_DIR = path.join(__dirname, "pet");
const PET_PREF_FILE = path.join(app.getPath("userData"), "pet.json");
const PET_PREF_VERSION = 3;   // v3：桌宠默认常驻（v1/v2 默认「仅变声时」，很多人从没见过它）
// 桌宠「录当前声音→挖掘音色」的单次内录时长（菜单文案与动作模块共用）
const CAPTURE_SECONDS = 15;
let petWin = null;
let petPref = { v: PET_PREF_VERSION, mode: "always", guide: true, x: null, y: null }; // always=常驻 | cascade=仅变声时
let petUserHidden = false; // 用户右键隐藏后，等下次级联启动再出现
let petHideTimer = null;
let petGuideUntil = 0;      // 页面导览的展示截止时间戳（ms）
let petLastGuide = null;    // 最近一次导览内容，供右键「再讲一遍本页」复用
let petReady = false;       // pet.html 是否已加载完（加载完成前导览先排队）
let petPendingGuide = null; // 加载完成前收到的导览请求

function getPetWin() {
  return petWin;
}

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
    lines: [`已松开 Alt，语音应已发出（${data.duration_s || "?"}秒）`, "去微信确认一下"],
    action: "play", motion: moves[Math.floor(Math.random() * moves.length)], duration: 9000,
  });
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

// 主窗口关闭即退出（桌宠不独立驻留）：销毁桌宠让 window-all-closed 生效
function destroyPet() {
  if (petWin) { petWin.destroy(); petWin = null; }
}

function createPetWindow(actions = {}) {
  petPref = loadPetPref();
  const { workArea } = screen.getPrimaryDisplay();
  const width = 220, height = 480;
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

  // 快捷面板：单击角色发最近合成 / 文字合成后发送 / 实时变声开关（动作由注入对象提供）
  ipcMain.on("pet:send-last", () => actions.sendWechatVoiceFromPet && actions.sendWechatVoiceFromPet());
  ipcMain.on("pet:send-text", (_e, text, voiceId) => actions.sendWechatTextFromPet && actions.sendWechatTextFromPet(String(text || "").trim(), String(voiceId || "")));
  ipcMain.on("pet:send-wav", (_e, wav) => actions.sendWechatWav && actions.sendWechatWav(String(wav || "")));   // 历史记录重发
  ipcMain.on("pet:preview", (_e, text, voiceId) => actions.previewWechatTextFromPet && actions.previewWechatTextFromPet(String(text || "").trim(), String(voiceId || "")));
  ipcMain.on("pet:live-toggle", () => actions.toggleLiveFromPet && actions.toggleLiveFromPet());

  // 右键菜单：显隐策略 + 页面导览 + 隐藏
  petWin.webContents.on("context-menu", () => {
    if (!petWin) return;
    Menu.buildFromTemplate([
      { label: "发送微信语音（用最近合成）",
        click() { actions.sendWechatVoiceFromPet && actions.sendWechatVoiceFromPet(); } },
      { label: "手动发变声语音（自己说话）",
        click() { actions.manualWechatFromPet && actions.manualWechatFromPet(); } },
      { label: `录制当前声音 → 挖掘音色（${CAPTURE_SECONDS}秒）`,
        click() { actions.captureMineFromPet && actions.captureMineFromPet(); } },
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

module.exports = {
  CAPTURE_SECONDS,
  createPetWindow,
  getPetWin,
  showPetGuide,
  petGuideFail,
  petGuideSent,
  destroyPet,
};
