#!/usr/bin/env node
/**
 * test-electron-load.cjs —— 用 electron 桩 require 全部 electron/*.cjs，
 * 任何一个在 **require 阶段** 抛异常就 FAIL。
 *
 * 为什么需要它（2026-09-12 事故）：
 *   alt-hint.cjs 拆文件时漏了 require("fs")，却用了 fs.existsSync。开发模式没暴露，
 *   **打包后主进程 require 阶段即崩**（ReferenceError: fs is not defined），
 *   用户装了 0.2.1/0.2.2 直接打不开。
 *
 *   静态检查（check-require.cjs）能抓这一类，但抓不住它的同类变体：
 *     - require 了不存在的模块路径（拼错文件名）
 *     - 顶层求值期访问 undefined（如 const X = someMissingThing.foo）
 *     - 模块顶层副作用崩溃
 *   桩加载是**直接验证**：把模块真跑一遍 require，比任何静态扫描都硬。
 *
 * 手法（见 .workbuddy-ai/memory 记录的「electron 桩装配冒烟」）：
 *   在 require 之前把 "electron" 解析到一个桩模块，桩里 app.getPath 等全用空实现。
 *   沙箱内起不了真 Electron GUI（Chromium GPU 进程必崩），所以只能这样验主进程代码。
 *
 * 注意：本测试**只验 require 阶段不崩**，不验运行时行为（那要真 GUI）。
 *
 * 运行：node tools/test-electron-load.cjs
 */

const Module = require("module");
const path = require("path");
const os = require("os");
const fs = require("fs");

const ROOT = path.join(__dirname, "..");
const ELECTRON_DIR = path.join(ROOT, "web", "electron");

// ---------------------------------------------------------------------------
// 排除名单：不是「被 require 的模块」，而是自带 main 流程的脚本。
// require 它们会真跑一遍测试/构建流程，与「模块能否加载」无关。
// ---------------------------------------------------------------------------
const SKIP = new Set([
  "smoke-loadpath.cjs",   // 自带断言流程，load 即执行（它自己就是 smoke 入口）
]);

// ---------------------------------------------------------------------------
// electron 桩：覆盖本仓库主进程用到的 API
// ---------------------------------------------------------------------------
function makeElectronStub() {
  const noop = () => {};
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "vm_electron_stub_"));

  class StubBrowserWindow {
    constructor() { this.webContents = makeWebContents(); }
    loadURL() { return Promise.resolve(); }
    loadFile() { return Promise.resolve(); }
    on() { return this; }
    once() { return this; }
    show() {}
    showInactive() {}
    hide() {}
    close() {}
    focus() {}
    isDestroyed() { return false; }
    isVisible() { return true; }
    setAlwaysOnTop() {}
    setIgnoreMouseEvents() {}
    setBounds() {}
    setPosition() {}
    setSize() {}
    getBounds() { return { x: 0, y: 0, width: 800, height: 600 }; }
    setMenu() {}
    setTitle() {}
    setResizable() {}
    setSkipTaskbar() {}
  }

  function makeWebContents() {
    return {
      once: () => {},
      on: () => {},
      send: () => {},
      executeJavaScript: () => Promise.resolve(),
      openDevTools: () => {},
      isLoading: () => false,
      setWindowOpenHandler: () => {},
      session: { setPermissionRequestHandler: () => {}, on: () => {} },
    };
  }

  const app = {
    getPath: (name) => {
      if (name === "userData") return path.join(tmp, "userData");
      if (name === "temp") return os.tmpdir();
      if (name === "home") return os.homedir();
      return tmp;
    },
    getName: () => "voice-morph-desktop",
    getVersion: () => "0.0.0-stub",
    isPackaged: false,
    on: noop,
    once: noop,
    whenReady: () => Promise.resolve(),
    quit: noop,
    exit: noop,
    requestSingleInstanceLock: () => true,
    hasSingleInstanceLock: () => true,
    setAppUserModelId: noop,
    setAsDefaultProtocolClient: () => true,
    disableHardwareAcceleration: noop,
    getLocale: () => "zh-CN",
    getSystemLocale: () => "zh-CN",
    setLoginItemSettings: noop,
    getLoginItemSettings: () => ({ openAtLogin: false }),
    relaunch: noop,
    focus: noop,
    hide: noop,
    show: noop,
    dock: { show: noop, hide: noop },
    commandLine: { appendSwitch: noop, hasSwitch: () => false },
  };

  return {
    app,
    BrowserWindow: StubBrowserWindow,
    screen: {
      getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 }, size: { width: 1920, height: 1080 } }),
      getAllDisplays: () => [{ workArea: { x: 0, y: 0, width: 1920, height: 1080 }, id: 1 }],
      getDisplayNearestPoint: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }),
      on: noop,
    },
    globalShortcut: { register: () => true, unregister: noop, unregisterAll: noop, isRegistered: () => false },
    ipcMain: { handle: noop, on: noop, once: noop, removeHandler: noop, removeAllListeners: noop },
    shell: { openExternal: () => Promise.resolve(), openPath: () => Promise.resolve(""), showItemInFolder: noop, beep: noop, trashItem: () => Promise.resolve(true) },
    dialog: {
      showMessageBox: () => Promise.resolve({ response: 0, checkboxChecked: false }),
      showOpenDialog: () => Promise.resolve({ canceled: true, filePaths: [] }),
      showSaveDialog: () => Promise.resolve({ canceled: true, filePath: "" }),
      showErrorBox: noop,
    },
    Menu: {
      buildFromTemplate: () => ({ popup: noop, items: [], getMenuItemById: () => null, append: noop, insert: noop }),
      setApplicationMenu: noop,
      getApplicationMenu: () => null,
    },
    MenuItem: class { constructor(o) { Object.assign(this, o); } },
    Tray: class { constructor() {} setToolTip() {} setContextMenu() {} on() {} destroy() {} },
    nativeImage: { createFromPath: () => ({ toPNG: () => Buffer.alloc(0), isEmpty: () => true }), createFromBuffer: () => ({}) },
    clipboard: { writeText: noop, readText: () => "" },
    Notification: class { constructor() {} show() {} close() {} },
    session: { defaultSession: { on: noop, setPermissionRequestHandler: noop } },
    powerMonitor: { on: noop },
    webContents: { getFocusedWebContents: () => null },
    crashReporter: { start: noop },
    systemPreferences: { get: () => null, on: noop },
    nativeTheme: { shouldUseDarkColors: false, on: noop },
    contextBridge: { exposeInMainWorld: noop },
  };
}

// ---------------------------------------------------------------------------
// 把 "electron" 解析到桩
// ---------------------------------------------------------------------------
let stubInstalled = false;
function installElectronStub() {
  if (stubInstalled) return;
  stubInstalled = true;
  const stubId = "__electron_stub__";
  require.cache[stubId] = {
    id: stubId,
    filename: stubId,
    loaded: true,
    exports: makeElectronStub(),
  };
  const origResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...rest) {
    if (request === "electron") return stubId;
    return origResolve.call(this, request, ...rest);
  };
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
function main() {
  installElectronStub();

  const files = fs
    .readdirSync(ELECTRON_DIR)
    .filter((f) => f.endsWith(".cjs"))
    .sort();

  const results = [];
  let failed = 0;
  let skipped = 0;

  console.log("");
  console.log("===== test-electron-load =====");
  console.log("（electron 桩装配；只验 require 阶段不崩，不验运行时行为）");
  console.log("");

  for (const f of files) {
    if (SKIP.has(f)) {
      skipped++;
      results.push(`[SKIP] ${f.padEnd(26)} -- 自带 main 流程，不作模块加载`);
      continue;
    }
    const full = path.join(ELECTRON_DIR, f);
    let ok = true;
    let detail = "ok";
    try {
      // 每个模块单独 require；已加载过的走缓存也没关系（同一次进程里行为一致）
      require(full);
    } catch (e) {
      ok = false;
      failed++;
      const frame = (e.stack || "").split("\n").find((l) => l.includes(f)) || "";
      detail = `${e.constructor.name}: ${e.message}${frame ? "  @" + frame.trim() : ""}`;
    }
    results.push(`[${ok ? "PASS" : "FAIL"}] ${f.padEnd(26)} -- ${detail}`);
  }

  for (const r of results) console.log(r);
  console.log("");
  console.log(`合计 ${files.length} 个文件：${files.length - failed - skipped} 加载成功，${failed} 失败，${skipped} 跳过`);
  if (failed) {
    console.log("RESULT: FAIL");
    console.log("");
    console.log("说明：require 阶段就崩 = 打包后主进程启动即崩（用户装了打不开）。");
    console.log("典型原因：漏 require、require 了不存在的路径、顶层求值期访问 undefined。");
    return 1;
  }
  console.log("RESULT: PASS");
  return 0;
}

if (require.main === module) {
  process.exit(main());
}

module.exports = { installElectronStub, makeElectronStub, SKIP };
