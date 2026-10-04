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
const altHint = require("./alt-hint.cjs");
const sfxWindow = require("./sfx-window.cjs");
const setup = require("./setup-ipc.cjs");
const { registerUpdateIpc, scheduleStartupUpdateCheck } = require("./update-ipc.cjs");

// 主窗口引用（setup IPC 弹目录选择器时需要 parent window，保证对话框置顶居中）
let mainWindow = null;

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
      // 2026-09-13 恢复默认：此前这里是 webSecurity:false + allowRunningInsecureContent:true，
      // 理由是「file:// 页面 fetch http://127.0.0.1 会被拦」。该结论已过时——后端
      // server.py 的 LOCAL_ORIGIN_RE 明确放行 `null` / `file://` 来源并回显 ACAO
      // （实测见 m2_server/tests/test_cors_file_origin.py），fetch 走标准 CORS 即可通过。
      // 关掉 webSecurity 等于让渲染层失去同源策略，任何 XSS 都能直读本地接口，
      // 对一个要开源的桌面应用是明确的负债，故不再需要。
    },
  });

  // 主窗口关闭即退出（桌宠不独立驻留）：销毁桌宠让 window-all-closed 生效
  win.on("closed", () => {
    if (mainWindow === win) mainWindow = null;
    pet.destroyPet();
    // 置顶提示横幅也必须销毁：它只要还活着（哪怕 hide 了）就算"还有窗口"，
    // window-all-closed 永不触发 → app.quit() 永不执行 → 应用无声无息留在后台
    // （桌宠已销毁、任务栏也没图标，只能靠任务管理器杀）。
    altHint.destroyAltHint();
    // 悬浮声板窗同理：它平时是 hide 状态，但窗口对象还在 → 同样会钉住 window-all-closed
    sfxWindow.destroySfxWindow();
    // 兜底：万一还有漏网的窗口（历史上就是这么漏掉的），直接退。
    // 绝不留"看不见关不掉"的后台幽灵进程 —— 用户只能开任务管理器杀，体验极差。
    if (process.platform !== "darwin" && BrowserWindow.getAllWindows().length === 0) {
      app.quit();
    }
  });

  // 加载规则（2026-09-12 修坑：生产版绝不能读 D:\变声 源码构建）：
  //   - 生产（安装版）：只加载安装包内 extraResources 的 web_dist 单候选，无 asar 回退。
  //     自动更新装完新包后，这里读到的一定是新前端；实际路径打印到日志，便于排查。
  //   - 开发（源码版）：仅当显式设置 ELECTRON_IS_DEV=1 且 5173 在跑时加载 Vite dev server；
  //     否则源码根 web/dist 优先，回退 asar 内置产物。
  //     这样可避免「遗留的 dev server 偷偷接管打包应用导致离线」的陷阱。
  if (!app.isPackaged) {
    if (process.env.ELECTRON_IS_DEV === "1" && (await backend.portInUse(5173))) {
      console.log("[frontend] 开发模式：加载 Vite dev server http://localhost:5173");
      win.loadURL("http://localhost:5173");
    } else {
      const html = backend.frontendHtmlCandidates().find((p) => fs.existsSync(p));
      console.log(`[frontend] 开发模式：加载 ${html || "未找到构建产物 index.html"}`);
      if (html) {
        win.loadFile(html);
      } else {
        console.error("[frontend] 未找到构建产物 dist/index.html");
      }
    }
  } else {
    const html = backend.frontendHtmlCandidates().find((p) => fs.existsSync(p));
    console.log(`[frontend] 生产模式：加载 ${html}（resourcesPath=${process.resourcesPath}）`);
    if (html) {
      win.loadFile(html);
    } else {
      console.error("[frontend] 生产模式未找到安装包内前端 web_dist/index.html（安装包不完整？）");
    }
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
  setup.registerSetupIpc(() => mainWindow);

  // 首次启动引导：只在「安装版 + 用户没处理过 + 确实缺模型」时弹一次。
  // 放在 startBackend 之前 —— 用户选完目录后紧接着的 spawn 就能带上新的 VM_*，
  // 不必先起一个注定缺模型的后端再重启。用户点「稍后配置」也照样往下走，不阻塞启动。
  let setupChanged = false;
  if (app.isPackaged) {
    try {
      setupChanged = await setup.runFirstRunGuide(null);
    } catch (e) {
      console.error("[setup] 首启引导异常（忽略，继续启动）:", e && e.message);
    }
  } else {
    // 开发态也把当前检测结果打进日志，便于对着设置面板排查
    const st = setup.currentStatus();
    console.log(`[setup] 模型检测：TTS=${st.ttsOk ? "ok" : "缺失"} RVC=${st.rvcOk ? "ok" : "缺失"}`
      + ` 缺失项=[${st.missing.join(",")}] 配置=${st.configPath}`);
  }

  const startInfo = await backend.startBackend(root);
  const win = await createWindow(root);
  mainWindow = win;
  pet.createPetWindow(petActions);
  // 悬浮特效声板窗：只注册 IPC 与热键（Ctrl+Alt+S），窗口按需创建、默认不出现
  sfxWindow.registerSfxIpc();
  backend.registerBackendIpc();
  registerCascadeHotkey();
  registerPhraseHotkeys();
  registerUpdateIpc();
  // 启动 12s 后静默检查一次更新，有新版才弹更新页（失败全程静默）
  scheduleStartupUpdateCheck(win);
  // 后端探测放在窗口之后异步进行，不阻塞界面出现；探不到才弹提示
  void backend.reportBackendTrouble(startInfo || {});
  if (setupChanged) {
    console.log("[setup] 首启引导已写入模型配置；重启后端使 VM_* 生效");
    void backend.restartBackend(root);
  }

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

/**
 * 快捷短语板的热键前缀：`Ctrl+Alt+1` ~ `Ctrl+Alt+5`。
 *
 * ★★ 为什么**不能**用裸 `Alt+N`（2026-10-04 实机定位，两个独立的坑同时踩）：
 *
 *   1. **菜单栏吃掉 Alt**：窗口是 `autoHideMenuBar: true`（第 29 行）—— 菜单栏
 *      只是「隐藏」而不是「不存在」，按 `Alt` 会先把菜单栏激活再谈别的。
 *      现场表现：按 Alt+1 弹出的是「File / Edit / View / Window / Help」那一栏，
 *      而 `globalShortcut` 根本没机会拿到这个组合。**这条是用户报障的直接原因。**
 *   2. **与微信「按住说话」冲突**：微信 PC 的说话键就是 Alt（见
 *      `sfx-window.cjs:38-44` 与 `:107` 的同一条铁律：「裸 Alt 或 Alt+X 会打架，
 *      Ctrl+Alt+X / Shift+Alt+X 不会」）。老师按住 Alt 说话时再按 1，两头都乱。
 *
 * 换成 `Ctrl+Alt+N` 后：有 Ctrl 修饰 → 菜单栏不抢；不是单 Alt → 微信不抢。
 * 与既有热键 `Ctrl+Alt+V`（级联）、`Ctrl+Alt+S`（声板）同一命名族，好记。
 */
const PHRASE_HOTKEY_PREFIX = "Control+Alt";

/** 全局热键 Ctrl+Alt+1 ~ Ctrl+Alt+5：快捷短语板第 N 条「直接播进虚拟声卡」。
 *  场景：PPT 放映 / 钉钉直播全屏时按一下，克隆音色替老师念那句课堂高频语，
 *  不用切回本应用 —— 所以播放必须在后端（POST /voice-phrases/{id}/play，
 *  走声板常驻播放器进 CABLE），浏览器 <audio> 跟着窗口焦点走，指望不上。
 *  每次触发现取 /api/voice-phrases（本地毫秒级，**不缓存**：面板里改了/删了/
 *  调了顺序，热键立即跟上，绝不念出已删除的旧短语）。不足 N 条时静默 no-op。
 *  冷态首次合成可能超过 httpJson 的 8s 超时：超时只是热键侧拿不到响应，
 *  后端仍会把合成播完 —— 第二次按就是零等待，可接受（面板里先点一次预热更好）。
 */
async function playPhraseByHotkey(n) {
  const r = await backend.httpJson("GET", "/api/voice-phrases");
  const items = r && r.json && Array.isArray(r.json.items) ? r.json.items : [];
  const item = items[n - 1];
  if (!item || !item.id) return;
  await backend.httpJson("POST", `/api/voice-phrases/${encodeURIComponent(item.id)}/play`, {});
}

function registerPhraseHotkeys() {
  for (let n = 1; n <= 5; n++) {
    const ok = globalShortcut.register(`${PHRASE_HOTKEY_PREFIX}+${n}`, () => {
      void playPhraseByHotkey(n);
    });
    if (!ok) console.error(`[hotkey] ${PHRASE_HOTKEY_PREFIX}+${n} 注册失败（可能被其它程序占用）`);
  }
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
  altHint.destroyAltHint();       // 兜底：任何退出路径都不留置顶横幅
  sfxWindow.destroySfxWindow();   // 同理：隐藏的声板窗也会钉住退出
  globalShortcut.unregisterAll(); // 全局热键随应用退出释放
  // 会话产物（outputs/.session/）「退出即删」的第三层：必须在 stopBackend **之前**
  // 发 —— 它是同步阻塞的最多 1.5s 请求，后端一被强杀就发不出去了。
  // 失败不阻塞退出：另外两层（启动清空 / uvicorn 关停）各自兜底。
  try {
    backend.purgeSessionSync();
  } catch {
    /* 忽略 */
  }
  backend.stopBackend(); // 关闭后端，绝不残留
});

// 关闭最后一个窗口时，连同后端一起退出（打包桌面应用标准行为）
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    backend.stopBackend();
    app.quit();
  }
});
